"""Import/export of a portable modlist document.

`untracked` records what the exporting machine was running that this manager
didn't install. It is a SEPARATE key, never entries in `mods`, and that's
load-bearing rather than tidiness: `mods` is parsed as id/id@version by
TavernLib's ModsListEntry too, so a "CoolMod.dll" in there would be resolved
against the trusted repos on every headless boot - failing every time, or
worse, matching some unrelated mod that happens to share the name. Kept out of
`mods`, it can only ever be read by something that asked for it. Purely
informational: an importer displays it so a pack is reproducible by hand, and
never installs, resolves, or disables anything from it."""
from dataclasses import dataclass

from tavern_shared.mods.errors import ModManagerError
from tavern_shared.mods.install import (
    collect_library_dependencies, disable_mod, install_library_dependency,
    install_mod, list_installed_mods, list_untracked_mods,
)
from tavern_shared.mods.pins import _parse_pin_entry
from tavern_shared.mods.repos import list_repos_named
from tavern_shared.mods.resolve import (
    _fetch_manifest, resolve_dependencies, resolve_mod_by_id,
)


MODLIST_SCHEMA = 1


def export_modlist(game_dir, cfg, pin_versions=False):
    """Every currently-ENABLED installed mod, in the canonical modlist shape.
    pin_versions=False (default) writes each as a bare id, so importing it
    later tracks whatever's latest at that point (a living "modpack"
    reference); pin_versions=True writes "id@version" for every entry, an
    exact reproducible snapshot of what's installed right now. `repos` is
    list_repos_named(cfg)'s shorthands - this launcher's configured sources,
    for a human reading the file or a pack curator documenting it.

    Enabled untracked mods go in `untracked` as {"name", "kind"}, so a modlist
    describes the machine honestly instead of quietly under-reporting it. Same
    enabled-only rule as `mods`, for the same reason: the file records the
    active set, not everything present on disk. All an exporter can honestly say
    about one is what it's called and what shape it is - there's no id, version,
    or source to record, which is exactly why no importer can act on it."""
    entries = []
    for m in sorted(list_installed_mods(game_dir), key=lambda r: r["id"]):
        if not m.get("enabled"):
            continue
        entries.append(f"{m['id']}@{m['version']}" if pin_versions else m["id"])
    untracked = [{"name": u["name"], "kind": u["kind"]}
                 for u in sorted(list_untracked_mods(game_dir),
                                 key=lambda u: u["name"].lower())
                 if u["enabled"]]
    return {
        "schema": MODLIST_SCHEMA,
        "repos": sorted(list_repos_named(cfg).keys()),
        "mods": entries,
        "untracked": untracked,
    }


@dataclass
class ImportPlan:
    roots: list           # list[ModManifest], one per resolved modlist entry
    dependencies: list     # list[ModManifest], resolve_dependencies' closure
    unresolved: list       # [(mod_id, version_or_None)] - not found in any configured repo
    hinted_repos: list     # the modlist's own informational `repos` field, for display
    untracked: list        # the modlist's `untracked` entries, for display ONLY - never
                           # installed, resolved, or matched against what's on disk. What
                           # the exporting machine ran that nothing can reproduce
                           # automatically, so the user knows what's left to do by hand.

    @property
    def blocking(self):
        """True if anything the modlist listed couldn't be found at all -
        apply_import refuses to run until this is empty, same as plan_join's
        `missing` blocks a join."""
        return bool(self.unresolved)


def import_modlist(modlist, index, repo_bases, side):
    """Resolves a modlist's `mods` entries against the importer's OWN
    configured repos only (repo_bases) - the file's `repos` field is never
    treated as a source to fetch from or silently add. Same
    by-id / exact-version resolution install already uses. An entry not
    resolvable from any configured repo is reported unresolved (with the
    modlist's hinted `repos` surfaced alongside it, so the user knows what to
    go add) rather than silently skipped or auto-added from its shorthand.

    `side` is the importing launcher's own, since the manager window this runs
    from is shared by both. A modlist exported on a client can name mods whose
    dependencies are client-only; importing it on a server resolves the same
    entries but leaves those dependencies out.

    An `untracked` block is carried through to the plan for display and is
    otherwise inert: nothing in it is resolved, installed, or compared against
    what's on disk. There's no id or version in one to resolve, and inventing a
    match by filename is exactly the guess that would install the wrong thing."""
    if modlist.get("schema") != MODLIST_SCHEMA:
        raise ModManagerError(
            f"This modlist is schema {modlist.get('schema')!r}; this launcher "
            f"only understands schema {MODLIST_SCHEMA}.")

    roots = []
    unresolved = []
    for entry in modlist.get("mods", []):
        mod_id, version = _parse_pin_entry(entry)
        try:
            roots.append(_fetch_manifest(repo_bases, mod_id, version) if version
                        else resolve_mod_by_id(repo_bases, mod_id))
        except ModManagerError:
            unresolved.append((mod_id, version))

    def fetch_manifest(mod_id, ver, source_repo):
        return _fetch_manifest(repo_bases, mod_id, ver, prefer_repo=source_repo)

    dependencies = resolve_dependencies(roots, index, fetch_manifest, side) if roots else []

    # Surfaced at plan time so the confirmation the user is shown is one that
    # can actually be carried out, rather than one that dies partway through
    # apply_import. Skipped when something is unresolved: that error names a
    # mod the user has to go and find a source for, which is the thing to fix
    # first, and a blocking plan never reaches apply_import anyway.
    if not unresolved:
        collect_library_dependencies(roots + dependencies)

    untracked = [u for u in (modlist.get("untracked") or []) if u.get("name")]
    return ImportPlan(roots=roots, dependencies=dependencies, unresolved=unresolved,
                      hinted_repos=list(modlist.get("repos", [])),
                      untracked=untracked)


def modlist_import_disables(game_dir, plan):
    """Every currently-ENABLED installed mod that isn't part of `plan`'s
    resolved set (roots + dependencies) - what apply_import will disable, so
    a caller can preview/confirm it before the import actually runs rather
    than only discovering it mid-install."""
    keep_ids = {mod.id for mod in plan.roots + plan.dependencies}
    return [rec["id"] for rec in list_installed_mods(game_dir)
            if rec["id"] not in keep_ids and rec.get("enabled")]


def apply_import(game_dir, plan, on_progress=None):
    """Installs everything import_modlist resolved - every root plus their
    dependency closure plus every collected library - in one confirmed batch
    (install_mod_closure's flow, generalized to a modlist's many roots), then
    disables every currently-ENABLED installed mod that isn't part of that
    resolved set (modlist_import_disables), so importing a modlist makes it
    the active set rather than just adding to whatever was already enabled.
    Disabled, not uninstalled: same as disable_mod, files stay in place. A
    dependency pulled in for another root counts as "in the list" even
    though it has no entry of its own in `mods`, so it's never disabled out
    from under the root that needs it.
    Raises if plan.blocking; callers must check that themselves first, same
    as render_active_set does for a JoinPlan."""
    if plan.blocking:
        raise ModManagerError(
            "This modlist has mods that couldn't be resolved from any "
            "configured source; fix that before importing it.")
    progress = on_progress or (lambda *_a: None)
    all_mods = plan.roots + plan.dependencies
    # Collected BEFORE the first install, not between the two loops: this
    # raises on a library conflict (two mods pinning the same filename at
    # different hashes), and doing it afterwards meant every mod was already on
    # disk by the time it did - a half-applied import, mods installed but the
    # disable pass never reached. Same check-then-act rule install_mod_closure
    # follows for exactly this reason.
    libs = collect_library_dependencies(all_mods)
    for mod in all_mods:
        progress(f"Installing {mod.id} {mod.version}")
        install_mod(game_dir, mod, progress)
    for lib in libs:
        progress(f"Installing library {lib.filename}")
        install_library_dependency(game_dir, lib, progress)

    for mod_id in modlist_import_disables(game_dir, plan):
        progress(f"Disabling {mod_id}")
        disable_mod(game_dir, mod_id)
