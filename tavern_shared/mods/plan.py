"""Turning a server's mod list into an active set: what to install, update,
enable, or deactivate for this join, and rendering that plan onto disk."""
import os
from dataclasses import dataclass

from tavern_shared.mods.cache import (
    _cache_library_dir, _cache_mod_dir, cache_restore_library,
    cache_restore_mod, cache_store_library, cache_store_mod,
)
from tavern_shared.mods.errors import ModManagerError
from tavern_shared.mods.install import (
    _read_mod_record, _read_sidecar, collect_library_dependencies, disable_mod,
    enable_mod, install_library_dependency, install_mod, list_installed_mods,
)
from tavern_shared.mods.layout import (
    _disabled_record_path, _library_sidecar_path, _safe_basename,
    _userlibs_dir,
)
from tavern_shared.mods.manifest import parity_required
from tavern_shared.mods.pins import _parse_pin_entry
from tavern_shared.mods.repos import _norm, _repo_shorthand
from tavern_shared.mods.resolve import _fetch_manifest, resolve_dependencies
from tavern_shared.mods.version import _parse_version


@dataclass
class PlanEntry:
    """One mod in the computed active set."""
    mod_id: str
    version: str
    source_repo: str
    reason: str        # "required" (server needs it) | "dependency" | "pinned"
    cached: bool       # already in the local cache at this exact version
    active: bool       # Mods/<id>/ is already enabled at exactly this version - a no-op
    manifest: object   # the resolved ModManifest - render_active_set installs straight


@dataclass
class JoinPlan:
    entries: list           # list[PlanEntry] - the full computed active set
    to_deactivate: list      # mod ids enabled in Mods/ that aren't in the active set
    libraries: list         # list[LibraryDependency] the active set needs
    missing: list           # [(mod_id, version)] required by the server, unresolvable from
                             # any repo the client has configured - blocks launching
    needs_repo: list        # [(mod_id, version, source_repo)] resolvable, but only from a
                             # repo the client hasn't added - not blocking on
                             # its own; surfaced so the user can add the repo and re-check
    pin_conflicts: list      # [(mod_id, pinned_version, server_version)] - a pinned mod's
                             # exact version disagrees with what the server requires; the
                             # user decides whether to unpin or accept the server's version
    declined: list = None    # [(mod_id, version)] mods this server runs and doesn't require
                             # that the user has already said no to. Not in the active set
                             # and not touched on disk; carried so the comparison can show
                             # them and let the choice be taken back

    def __post_init__(self):
        if self.declined is None:
            self.declined = []

    @property
    def blocking(self):
        """True if this plan can't safely be applied at all - some mod the
        server actually requires isn't resolvable from anywhere the client
        knows of. needs_repo alone never blocks: it's
        information for the user to act on, not a launch stopper by itself."""
        return bool(self.missing)


def plan_join(game_dir, server_mods, index, repo_bases, pinned=None, declined=None):
    """Computes active = required(server) UNION recommended(server, not declined)
    UNION resolve_dependencies(those) UNION pinned, and diffs it against what's
    cached/active - the render PLAN, not the render itself.

    server_mods: entries as returned by the ping/pong handshake's full list
    (handshake_snapshot's mods_list, or the equivalent fetched over the wire) -
    {"id","version","client_side","server_side","parity_required","source_repo"}.
    Only client_side entries concern the client at all; server_side-only entries
    are ignored here. Of those, parity_required splits them two ways:

      true   the server enforces this exact version on join, so it goes in the
             active set and a client that can't resolve it is blocked.
      false  the server won't block over it. Still planned in by default, at the
             server's version, so the common case is that everyone matches - but
             the user can decline, and a declined mod is left out entirely
             rather than installed, updated, or deactivated.

    index: the client's own merged index (fetch_indexes(repo_bases)) - used to
    pick each dependency's version within a major.
    repo_bases: the client's configured repos (list_repos(cfg)) - the only
    repos anything is actually fetched from.
    pinned: "id" / "id@version" strings (the launcher's always-on set) -
    unioned in regardless of which server is being joined.
    declined: mod ids the user has already said no to for THIS server. Only a
    mod the server marks parity_required false can be declined; a declined id
    the server actually requires is ignored, since no client-side choice can
    make that join work."""
    pinned = pinned or []
    declined = set(declined or [])

    client_mods = [m for m in server_mods if m.get("client_side")]
    required = {m["id"]: m["version"] for m in client_mods
                if parity_required(m)}
    recommended = {m["id"]: m["version"] for m in client_mods
                   if not parity_required(m)}
    hinted_repo = {m["id"]: m.get("source_repo") for m in server_mods if m.get("source_repo")}

    # A decline only ever applies to a mod the server marks optional. Declining
    # something required would just move the rejection from here to the server.
    skipped = [(mid, ver) for mid, ver in recommended.items() if mid in declined]

    roots = []
    missing = []
    needs_repo = []
    reason = {}         # mod_id -> "required" | "recommended" | "pinned", first tag wins
    exact_pin = {}      # mod_id -> pinned version (None if pinned to latest)

    def resolve_root(mod_id, version, tag):
        try:
            manifest = _fetch_manifest(repo_bases, mod_id, version)
        except ModManagerError:
            hint = hinted_repo.get(mod_id)
            if hint and _norm(hint) not in [_norm(b) for b in repo_bases]:
                needs_repo.append((mod_id, version, hint))
            elif tag == "recommended":
                # Unresolvable and not actually required: silently doing without
                # it beats blocking or nagging over a mod the server allows the
                # client not to have.
                pass
            else:
                missing.append((mod_id, version))
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
                missing.append((mod_id, "latest"))
                continue
            version = max((v for s in matches for v in s.versions), key=_parse_version)
        resolve_root(mod_id, version, "pinned")

    pin_conflicts = [
        (mod_id, pinned_version, required[mod_id])
        for mod_id, pinned_version in exact_pin.items()
        if mod_id in required and pinned_version and pinned_version != required[mod_id]
    ]

    def fetch_manifest(mod_id, version, source_repo):
        return _fetch_manifest(repo_bases, mod_id, version, prefer_repo=source_repo)

    # "client" is fixed, not a parameter: this is the join flow, which only ever
    # runs in the client launcher, and the server's own side is resolved
    # independently by whatever installed its mods (the server launcher, or
    # TavernLib's ModReconciler on a headless host).
    dependencies = resolve_dependencies(roots, index, fetch_manifest, "client") if roots else []
    for dep in dependencies:
        reason.setdefault(dep.id, "dependency")

    all_mods = roots + dependencies
    installed = {rec["id"]: rec for rec in list_installed_mods(game_dir)}

    entries = []
    for mod in all_mods:
        cached = os.path.isdir(_cache_mod_dir(mod.id, mod.version))
        rec = installed.get(mod.id)
        active = bool(rec and rec.get("enabled") and rec.get("version") == mod.version)
        entries.append(PlanEntry(mod_id=mod.id, version=mod.version, source_repo=mod.source_repo,
                                  reason=reason.get(mod.id, "required"), cached=cached, active=active,
                                  manifest=mod))

    active_ids = {e.mod_id for e in entries}
    # Declining is "don't add it", never "take it away". A mod the user already
    # chose to have and the server doesn't require stays exactly as it is.
    to_deactivate = [mod_id for mod_id, rec in installed.items()
                      if rec.get("enabled") and mod_id not in active_ids
                      and mod_id not in declined]

    libraries = collect_library_dependencies(all_mods) if all_mods else []

    return JoinPlan(entries=entries, to_deactivate=to_deactivate, libraries=libraries,
                    missing=missing, needs_repo=needs_repo, pin_conflicts=pin_conflicts,
                    declined=skipped)


# Client-vs-server comparison
#
# A JoinPlan says what to DO. This says what the two sides currently look like
# and how each mod differs, which is what a person actually needs to see before
# agreeing to it. Pure: derived from a plan plus what's on disk, no display and
# no network, so it's testable on its own and the window below only renders it.

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
        """A mod this server runs but doesn't require. The user's call in either
        direction, and the reason a row can be overruled towards NOT installing
        something, rather than only towards keeping something."""
        return self.reason == "recommended" or self.action == ACTION_SKIPPED

    @property
    def downloads(self):
        """True when applying this row actually fetches bytes. The only part of
        a plan that takes real time, so it's what the summary counts and what
        each row is labelled with."""
        return (not self.cached) and self.action in (
            ACTION_INSTALL, ACTION_UPDATE, ACTION_DOWNGRADE)

    @property
    def blocking(self):
        return self.action == ACTION_MISSING


def build_mod_diff(game_dir, server_mods, plan):
    """The full comparison behind a JoinPlan: one DiffRow per mod on either
    side, sorted problems-first. server_mods is the handshake list; plan is what
    plan_join computed from it.

    Two kinds of row are marked optional, both because the server's parity check
    can't fail over them. Deactivations: a mod the server doesn't run at all can
    stay active here without affecting the join. Recommendations: a mod the
    server does run but doesn't require, which it won't block over either
    way. Everything else is what the server actually enforces, so it isn't
    presented as a choice."""
    installed = {r["id"]: r for r in list_installed_mods(game_dir)}
    required = {m["id"]: m.get("version", "") for m in server_mods if m.get("client_side")}
    conflicts = {mid: (pv, sv) for mid, pv, sv in plan.pin_conflicts}

    rows = []
    for e in plan.entries:
        rec = installed.get(e.mod_id)
        cv = rec.get("version", "") if rec else ""
        enabled = bool(rec.get("enabled")) if rec else False
        if e.active:
            action = ACTION_MATCH
        elif rec is None:
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
        if pv_sv:
            note = f"your pin says {pv_sv[0]}, the server requires {pv_sv[1]}"
        elif recommended:
            note = ("this server recommends it but doesn't require it; skipping "
                    "it won't stop you joining")
        else:
            note = ""
        rows.append(DiffRow(
            mod_id=e.mod_id, server_version=required.get(e.mod_id, ""),
            client_version=cv, client_enabled=enabled, action=action,
            reason=e.reason, optional=recommended and not e.active,
            source_repo=e.source_repo, note=note, cached=e.cached,
            name=e.manifest.name))

    for mod_id in plan.to_deactivate:
        rec = installed.get(mod_id, {})
        rows.append(DiffRow(
            mod_id=mod_id, server_version="", client_version=rec.get("version", ""),
            client_enabled=True, action=ACTION_DEACTIVATE, reason="", optional=True,
            source_repo=rec.get("source_repo", ""),
            note="this server doesn't run it; keeping it on won't block your join",
            name=rec.get("name", mod_id)))

    seen = {r.mod_id for r in rows}
    for mod_id, version in plan.declined:
        if mod_id in seen:
            continue
        rec = installed.get(mod_id, {})
        rows.append(DiffRow(
            mod_id=mod_id, server_version=version,
            client_version=rec.get("version", ""),
            client_enabled=bool(rec.get("enabled")), action=ACTION_SKIPPED,
            reason="recommended", optional=True,
            source_repo=rec.get("source_repo", ""),
            note="you've chosen not to install this one for this server",
            name=rec.get("name", mod_id)))
        seen.add(mod_id)

    for m in server_mods:
        if m["id"] in seen or m.get("client_side"):
            continue
        rows.append(DiffRow(
            mod_id=m["id"], server_version=m.get("version", ""), client_version="",
            client_enabled=False, action=ACTION_SERVER_ONLY, reason="",
            optional=False, source_repo=m.get("source_repo", ""),
            note="runs on the server only", name=m.get("name", m["id"])))
        seen.add(m["id"])

    for mod_id, version in plan.missing:
        if mod_id in seen:
            continue
        rec = installed.get(mod_id, {})
        rows.append(DiffRow(
            mod_id=mod_id, server_version=version, client_version=rec.get("version", ""),
            client_enabled=bool(rec.get("enabled")), action=ACTION_MISSING,
            reason="required", optional=False, source_repo="",
            note="not in any source you've added", name=rec.get("name", mod_id)))
        seen.add(mod_id)

    for mod_id, version, repo in plan.needs_repo:
        if mod_id in seen:
            continue
        rec = installed.get(mod_id, {})
        rows.append(DiffRow(
            mod_id=mod_id, server_version=version, client_version="",
            client_enabled=False, action=ACTION_NEEDS_REPO, reason="required",
            optional=False, source_repo=repo,
            note=f"add {_repo_shorthand(repo)} as a source, then check again",
            name=rec.get("name", mod_id)))
        seen.add(mod_id)

    # Sorted by display name, not id, so the on-screen order matches what's
    # actually shown - a mod_id-sorted list would look shuffled once the
    # window renders names instead of ids.
    return sorted(rows, key=lambda r: (_ACTION_ORDER.get(r.action, 9), r.name.lower()))


def _prune_orphaned_libraries(game_dir):
    """After the active set changes, removes any UserLibs/ library no longer
    pinned by a currently-ENABLED mod's record. Each library was already
    cache_store_library'd when its owning mod was installed/adopted, so
    nothing is lost - it just isn't present in UserLibs/ until something
    needs it again.

    Enabled rather than merely installed, because a disabled mod isn't loading
    and neither should its libraries be. Enabling it again restores them: the
    library pass runs over the whole active set, after the pass that enables."""
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
    """How many items render_active_set will work through for this plan: one per
    mod it changes, per library it checks, and per mod it deactivates. Exactly
    the set that reports through on_step, so a caller can size a progress bar
    before starting and have it end full."""
    return (len([e for e in plan.entries if not e.active])
            + len(plan.libraries) + len(plan.to_deactivate))


def render_active_set(game_dir, plan, on_progress=None, on_step=None):
    """Applies a JoinPlan to disk - the actual render step. Restores from
    cache wherever cached (a file move), downloads only what's
    genuinely missing, then deactivates anything no longer needed - never
    deleting a mod outright (it's cached first) and never re-downloading
    something already cached. Raises ModManagerError immediately if
    plan.blocking; callers must check that themselves before deciding whether
    to show a plan at all, same as install_mod_closure surfaces resolution
    errors before touching disk.

    on_progress takes a line of human-readable detail. on_step brackets each
    item with on_step(item_id, "start") and on_step(item_id, "done"): mod ids
    for mods, filenames for libraries, one pair per item counted by
    render_plan_steps. A library already active at the pinned content still
    reports both, so the count a caller sized against is always reached."""
    if plan.blocking:
        raise ModManagerError(
            "This plan has required mods that couldn't be resolved from any "
            "configured repo; fix that before applying it.")
    progress = on_progress or (lambda *_a: None)
    step = on_step or (lambda *_a: None)

    for entry in plan.entries:
        if entry.active:
            continue
        step(entry.mod_id, "start")
        rec = _read_mod_record(game_dir, entry.mod_id)
        disabled_here = (bool(rec) and rec.get("version") == entry.version
                         and os.path.isfile(_disabled_record_path(game_dir, entry.mod_id)))
        if disabled_here:
            # Already on disk at exactly this version, just switched off. Renaming
            # the record back is the whole job: no copy out of the cache, no
            # download, and it works with no network at all. The counterpart to
            # deactivating by disabling below.
            progress(f"Enabling {entry.mod_id} {entry.version}")
            enable_mod(game_dir, entry.mod_id)
        elif entry.cached:
            progress(f"Installing {entry.mod_id} {entry.version} (cached)")
            cache_restore_mod(game_dir, entry.mod_id, entry.version)
        else:
            progress(f"Downloading {entry.mod_id} {entry.version}")
            install_mod(game_dir, entry.manifest, progress)
            cache_store_mod(game_dir, entry.mod_id)
        step(entry.mod_id, "done")

    for lib in plan.libraries:
        filename = _safe_basename(lib.filename)
        step(filename, "start")
        dest = os.path.join(_userlibs_dir(game_dir), filename)
        existing = _read_sidecar(_library_sidecar_path(game_dir, filename))
        if os.path.isfile(dest) and existing and existing.get("sha256") == lib.sha256:
            step(filename, "done")
            continue     # already active with the exact pinned content
        if os.path.isdir(_cache_library_dir(filename, lib.sha256)):
            progress(f"Installing library {filename} (cached)")
            cache_restore_library(game_dir, filename, lib.sha256)
        else:
            progress(f"Downloading library {filename}")
            install_library_dependency(game_dir, lib, progress)
            cache_store_library(game_dir, filename)
        step(filename, "done")

    for mod_id in plan.to_deactivate:
        step(mod_id, "start")
        progress(f"Deactivating {mod_id}")
        # Disabled, not uninstalled: the record is renamed so MelonLoader skips
        # the folder, and every file stays exactly where it is. The mod keeps its
        # place in the installed list and comes back on with a rename rather than
        # a download. Cached first all the same, so a later version switch is a
        # file move whichever way it goes.
        cache_store_mod(game_dir, mod_id)
        disable_mod(game_dir, mod_id)
        step(mod_id, "done")

    _prune_orphaned_libraries(game_dir)


def resolve_missing_mods(missing, index, repo_bases):
    """Resolves a rejection payload's `missing` entries
    ({"id","version","source_repo"}) into installable manifests, for the
    post-reject recovery flow: after a join is denied for a mod mismatch, the
    launcher runs this to fetch exactly the mods+versions the server said
    were wrong, installs them, and rejoins. `source_repo` is a HINT only:
    resolution always goes through the client's own configured repos
    (repo_bases), never a URL
    taken from the payload; an entry resolvable nowhere raises naming its
    hint if it has one, same missing/needs_repo distinction plan_join uses.

    Returns (roots, dependencies): roots is one ModManifest per resolved
    missing entry, in order; dependencies is resolve_dependencies' closure
    over all of them (the same shape install_mod_closure computes for a
    single root, generalized to plan_join's multi-root case)."""
    roots = []
    unresolved = []
    for entry in missing:
        mod_id, version = entry["id"], entry["version"]
        try:
            roots.append(_fetch_manifest(repo_bases, mod_id, version))
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
