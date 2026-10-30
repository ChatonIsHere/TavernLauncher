"""Portable modlists: {schema, repos, mods, untracked}. `untracked` is display
only and kept out of `mods`, which TavernLib's headless config resolves on
every boot."""
from dataclasses import dataclass, field

from tavern_shared.mods.base import ModManagerError, _warn
from tavern_shared.mods.install import (
    _check_folder_libraries, _scan_mods, collect_library_dependencies,
    disable_mod, has_target, install_mod_set, list_installed_mods,
)
from tavern_shared.mods.pins import _parse_pin_entry
from tavern_shared.mods.repos import (
    DEFAULT_REPO, _repo_shorthand, _user_repos, replacement_of_unlisted,
)
from tavern_shared.mods.resolve import (
    _fetch_manifest, _index_source, resolve_dependencies, resolve_mod_by_id,
)


MODLIST_SCHEMA = 1


def export_modlist(game_dir, cfg, target, pin_versions=False):
    """The enabled mods installed for `target`, as bare ids or id@version."""
    installed, untracked = _scan_mods(game_dir)
    entries = [f"{m['id']}@{m['version']}" if pin_versions else m["id"]
               for m in sorted(installed, key=lambda r: r["id"])
               if m.get("enabled") and has_target(m, target)]
    untracked = [{"name": u["name"], "kind": u["kind"]}
                 for u in sorted(untracked, key=lambda u: u["name"].lower())
                 if u["enabled"]]
    return {
        "schema": MODLIST_SCHEMA,
        "repos": sorted({_repo_shorthand(DEFAULT_REPO), *_user_repos(cfg)}),
        "mods": entries,
        "untracked": untracked,
    }


@dataclass
class ImportPlan:
    roots: list             # list[ModManifest]
    dependencies: list      # list[ModManifest]
    unresolved: list        # [(mod_id, version or None)] no configured repo has
    hinted_repos: list      # the file's `repos`, display only
    untracked: list         # the file's `untracked`, display only
    target: str
    no_build: list          # [(mod_id, version)] left out: no build for target
    replaced: list = field(default_factory=list)   # [(old id, new id)] swapped in

    @property
    def blocking(self):
        return bool(self.unresolved)


def import_modlist(modlist, index, repo_bases, target, game_dir=None):
    """Resolves `mods` through the importer's own repos only; the file's
    `repos` is never fetched from. An entry with no `target` build is left
    out, so a server can import a client's list. An entry for a replaced mod
    takes its replacement's latest. Library conflicts surface here, before
    anything is confirmed."""
    if modlist.get("schema") != MODLIST_SCHEMA:
        raise ModManagerError(
            f"This modlist is schema {modlist.get('schema')!r}; this launcher "
            f"only understands schema {MODLIST_SCHEMA}.")

    roots, unresolved, no_build, replaced = [], [], [], []
    for entry in modlist.get("mods", []):
        mod_id, version = _parse_pin_entry(entry)
        if (rep := replacement_of_unlisted(index, mod_id)) is not None:
            replaced.append((mod_id, rep.by))
            mod_id, version = rep.by, None
        prefer = _index_source(index, mod_id, repo_bases, version)
        try:
            root = (_fetch_manifest(repo_bases, mod_id, version, prefer) if version
                    else resolve_mod_by_id(repo_bases, mod_id, prefer))
        except ModManagerError:
            unresolved.append((mod_id, version))
            continue
        if root.artifact_for(target) is None:
            _warn(f"modlist entry '{mod_id}' {root.version} has no {target} build; left out.")
            no_build.append((mod_id, root.version))
        else:
            roots.append(root)

    dependencies = resolve_dependencies(roots, index, repo_bases, target)
    # Skipped when unresolved: that's the thing to fix first.
    if not unresolved:
        libs = collect_library_dependencies(roots + dependencies, target)
        if game_dir is not None:
            _check_folder_libraries(game_dir, roots + dependencies, target, libs)

    return ImportPlan(roots=roots, dependencies=dependencies, unresolved=unresolved,
                      hinted_repos=list(modlist.get("repos", [])),
                      untracked=[u for u in (modlist.get("untracked") or []) if u.get("name")],
                      target=target, no_build=no_build, replaced=replaced)


def modlist_import_disables(game_dir, plan):
    """The enabled mods installed for the plan's target that apply_import
    will disable."""
    keep_ids = {mod.id for mod in plan.roots + plan.dependencies}
    return [rec["id"] for rec in list_installed_mods(game_dir)
            if rec["id"] not in keep_ids and rec.get("enabled")
            and has_target(rec, plan.target)]


def apply_import(game_dir, plan, on_progress):
    """Installs the plan's closure, then disables every other enabled mod,
    which includes any old mod a replacement in it stands in for."""
    if plan.blocking:
        raise ModManagerError(
            "This modlist has mods that couldn't be resolved from any "
            "configured source; fix that before importing it.")
    install_mod_set(game_dir, plan.dependencies + plan.roots, plan.target, on_progress)
    for mod_id in modlist_import_disables(game_dir, plan):
        on_progress(f"Disabling {mod_id}")
        disable_mod(game_dir, mod_id)
