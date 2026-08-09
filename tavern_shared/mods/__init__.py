"""Community mod manager.

Everything about fetching a mod index, resolving dependencies, and putting a
verified .dll on disk lives in this package. The launcher windows never talk to
GitHub, hash anything, or touch Mods/ for index-driven mods directly.

Two on-disk shapes are contracts any other installer of these mods must match
byte-for-byte: the per-mod install layout + its record, and by-id resolution
(latest.json / latest.<major>.json). Both live in `layout`; keep them stable.

Each mod installs into its OWN folder, Mods/<id>/, and its record is written as
Mods/<id>/manifest.json inside that folder. That filename is deliberate: recent
MelonLoader only scans a Mods/ subfolder that contains a manifest.json (it checks
existence only, never the content), so the record doubles as the marker that makes
the folder load. Uninstall is "delete the folder".

Libraries are separate: a mod's library_dependencies install FLAT into UserLibs/
(never bundled into a mod folder), each with its own UserLibs/<filename>.meta.json
sidecar. A mod's record lists the library filenames it pins, which is how
uninstall_mod reference-counts UserLibs/ files. Native/unmanaged DLLs MUST be
libraries, because MelonLoader only registers UserLibs/ as a native search path.

This module re-exports the logic surface so callers can do `from tavern_shared
import mods` and reach it all through one name. The windows are NOT re-exported
-- they live in `tavern_shared.mods.ui` (and client/core/mod_diff_window.py) and
are imported directly, so importing the logic never drags in tkinter.
"""
from tavern_shared.mods.errors import ModManagerError
from tavern_shared.mods.hostcfg import set_config_accessors
from tavern_shared.mods.layout import DISABLED_RECORD_NAME, RECORD_NAME
from tavern_shared.mods.manifest import (
    SUPPORTED_MANIFEST_MAJOR, LibraryDependency, ModManifest, ModSummary,
    parity_required,
)
from tavern_shared.mods.repos import (
    CFG_REPOS_KEY, DEFAULT_REPO, SUPPORTED_INDEX_MAJOR, add_repo, fetch_indexes,
    list_repos, list_repos_named, remove_repo,
)
from tavern_shared.mods.resolve import (
    MAX_DEPENDENCY_DEPTH, resolve_dependencies, resolve_mod_by_id,
)
from tavern_shared.mods.install import (
    collect_library_dependencies, disable_mod, disable_untracked_dll, enable_mod,
    enable_untracked_dll, handshake_snapshot, install_library_dependency,
    install_mod, install_mod_closure, list_installed_mods, list_mods,
    list_untracked_mods, mod_status, uninstall_mod,
)
from tavern_shared.mods.cache import (
    adopt_installed_mods, cache_restore_library, cache_restore_mod,
    cache_store_library, cache_store_mod, clear_mod_cache,
)
from tavern_shared.mods.pins import (
    CFG_DECLINED_KEY, CFG_PINNED_KEY, add_pin, list_declined, list_pinned,
    remove_pin, set_declined,
)
from tavern_shared.mods.plan import (
    ACTION_ACTIVATE, ACTION_DEACTIVATE, ACTION_DOWNGRADE, ACTION_ENABLE,
    ACTION_INSTALL, ACTION_MATCH, ACTION_MISSING, ACTION_NEEDS_REPO,
    ACTION_SERVER_ONLY, ACTION_SKIPPED, ACTION_UPDATE, DiffRow, JoinPlan,
    PlanEntry, build_mod_diff, plan_join, render_active_set, render_plan_steps,
    resolve_missing_mods,
)
from tavern_shared.mods.modlist import (
    MODLIST_SCHEMA, ImportPlan, apply_import, export_modlist, import_modlist,
    modlist_import_disables,
)

__all__ = [
    "ModManagerError", "set_config_accessors",
    "RECORD_NAME", "DISABLED_RECORD_NAME",
    "SUPPORTED_MANIFEST_MAJOR", "LibraryDependency", "ModManifest", "ModSummary",
    "parity_required",
    "DEFAULT_REPO", "CFG_REPOS_KEY", "SUPPORTED_INDEX_MAJOR", "list_repos",
    "list_repos_named", "add_repo", "remove_repo", "fetch_indexes",
    "MAX_DEPENDENCY_DEPTH", "resolve_dependencies", "resolve_mod_by_id",
    "install_mod", "install_mod_closure", "uninstall_mod", "disable_mod",
    "enable_mod", "mod_status", "list_mods", "list_installed_mods",
    "list_untracked_mods", "disable_untracked_dll", "enable_untracked_dll",
    "handshake_snapshot", "collect_library_dependencies",
    "install_library_dependency",
    "cache_store_mod", "cache_store_library", "clear_mod_cache",
    "adopt_installed_mods", "cache_restore_mod", "cache_restore_library",
    "CFG_PINNED_KEY", "CFG_DECLINED_KEY", "list_pinned", "list_declined",
    "set_declined", "add_pin", "remove_pin",
    "PlanEntry", "JoinPlan", "plan_join", "DiffRow", "build_mod_diff",
    "render_plan_steps", "render_active_set", "resolve_missing_mods",
    "ACTION_INSTALL", "ACTION_ACTIVATE", "ACTION_UPDATE", "ACTION_DOWNGRADE",
    "ACTION_ENABLE", "ACTION_DEACTIVATE", "ACTION_MATCH", "ACTION_SKIPPED",
    "ACTION_SERVER_ONLY", "ACTION_MISSING", "ACTION_NEEDS_REPO",
    "MODLIST_SCHEMA", "export_modlist", "ImportPlan", "import_modlist",
    "modlist_import_disables", "apply_import",
]
