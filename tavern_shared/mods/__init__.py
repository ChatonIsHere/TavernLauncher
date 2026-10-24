"""Community mod manager: fetching the index, resolving dependencies,
verified installs, the client cache and join planning. docs/mod-manager-design.md
is the map.

The package re-exports what the launchers reach as `mods.<name>`; everything
else is imported from its submodule. The windows live in
`tavern_shared.mods.ui` (and client/core/mod_diff_window.py), so importing the
logic never drags in tkinter."""
from tavern_shared.mods.errors import ModManagerError
from tavern_shared.mods.manifest import (
    mod_scenes, parity_required, runs_in_scene, scene_from_arg,
)
from tavern_shared.mods.repos import fetch_indexes, list_repos
from tavern_shared.mods.install import (
    collect_library_dependencies, handshake_snapshot, has_target,
    install_library_dependency, install_mod, install_mod_set, installed_targets,
    list_installed_mods, mod_status,
)
from tavern_shared.mods.cache import adopt_installed_mods
from tavern_shared.mods.pins import add_pin, list_declined, list_pinned, set_declined
from tavern_shared.mods.plan import (
    build_mod_diff, plan_join, render_active_set, resolve_missing_mods,
)
