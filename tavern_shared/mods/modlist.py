"""Import/export of a portable modlist: {schema, repos, mods, untracked}.

`untracked` is a separate key, never entries in `mods`: TavernLib's
ModsListEntry parses `mods` as id/id@version on every headless boot, so a
"CoolMod.dll" there would be resolved against the trusted repos. It is
display-only; nothing ever installs, resolves or disables from it."""
from dataclasses import dataclass

from tavern_shared.mods.errors import ModManagerError
from tavern_shared.mods.install import (
    _scan_mods, collect_library_dependencies, disable_mod, install_mod_set,
    list_installed_mods,
)
from tavern_shared.mods.pins import _parse_pin_entry
from tavern_shared.mods.repos import list_repos_named
from tavern_shared.mods.resolve import (
    _fetch_manifest, _index_source, resolve_dependencies, resolve_mod_by_id,
)


MODLIST_SCHEMA = 1


def export_modlist(game_dir, cfg, pin_versions=False):
    """The ENABLED installed mods as a modlist: bare ids (track latest) or, with
    pin_versions, "id@version". `repos` lists this launcher's shorthands for
    reference; `untracked` lists enabled untracked mods as {"name", "kind"}."""
    installed, untracked = _scan_mods(game_dir)
    entries = [f"{m['id']}@{m['version']}" if pin_versions else m["id"]
               for m in sorted(installed, key=lambda r: r["id"]) if m.get("enabled")]
    untracked = [{"name": u["name"], "kind": u["kind"]}
                 for u in sorted(untracked, key=lambda u: u["name"].lower())
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
    untracked: list        # the modlist's `untracked` entries, for display only

    @property
    def blocking(self):
        """True if any entry couldn't be found; apply_import refuses until then."""
        return bool(self.unresolved)


def import_modlist(modlist, index, repo_bases, side):
    """Resolves a modlist's `mods` through the importer's own repos only: the
    file's `repos` is never fetched from or added. Unresolvable entries land in
    `unresolved` beside the file's repo hints. `side` is the importing
    launcher's own, so a server leaves out client-only dependencies. Library
    conflicts surface here, before anything is confirmed. `untracked` is
    carried through for display only."""
    if modlist.get("schema") != MODLIST_SCHEMA:
        raise ModManagerError(
            f"This modlist is schema {modlist.get('schema')!r}; this launcher "
            f"only understands schema {MODLIST_SCHEMA}.")

    roots = []
    unresolved = []
    for entry in modlist.get("mods", []):
        mod_id, version = _parse_pin_entry(entry)
        prefer = _index_source(index, mod_id, repo_bases, version)
        try:
            roots.append(_fetch_manifest(repo_bases, mod_id, version, prefer) if version
                         else resolve_mod_by_id(repo_bases, mod_id, prefer))
        except ModManagerError:
            unresolved.append((mod_id, version))

    def fetch_manifest(mod_id, ver, source_repo):
        return _fetch_manifest(repo_bases, mod_id, ver, prefer_repo=source_repo)

    dependencies = resolve_dependencies(roots, index, fetch_manifest, side) if roots else []

    # Skipped when blocking: the unresolved entry is the thing to fix first.
    if not unresolved:
        collect_library_dependencies(roots + dependencies)

    untracked = [u for u in (modlist.get("untracked") or []) if u.get("name")]
    return ImportPlan(roots=roots, dependencies=dependencies, unresolved=unresolved,
                      hinted_repos=list(modlist.get("repos", [])),
                      untracked=untracked)


def modlist_import_disables(game_dir, plan):
    """The ENABLED mods outside the plan's roots and dependencies: what
    apply_import will disable, for a preview."""
    keep_ids = {mod.id for mod in plan.roots + plan.dependencies}
    return [rec["id"] for rec in list_installed_mods(game_dir)
            if rec["id"] not in keep_ids and rec.get("enabled")]


def apply_import(game_dir, plan, on_progress=None):
    """Installs the plan's closure (install_mod_set), then disables every enabled
    mod outside it, so the modlist becomes the active set. Dependencies count
    as in the list. Raises if plan.blocking."""
    if plan.blocking:
        raise ModManagerError(
            "This modlist has mods that couldn't be resolved from any "
            "configured source; fix that before importing it.")
    progress = on_progress or (lambda *_a: None)
    install_mod_set(game_dir, plan.dependencies + plan.roots, progress)

    for mod_id in modlist_import_disables(game_dir, plan):
        progress(f"Disabling {mod_id}")
        disable_mod(game_dir, mod_id)
