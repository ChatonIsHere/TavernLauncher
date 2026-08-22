from tavern_shared.orderable_registry import OrderableRegistry
from tavern_shared.paths import _tavern_data_dir
import os

_registry = OrderableRegistry(os.path.join(_tavern_data_dir(), "tavernkeeper_tab_order.json"))


def register_tab(key, label, build_fn, built_in=False, after=None):
    """build_fn(parent, window) builds the tab's contents. Core tabs and
    addon tabs register the exact same way -- the only difference is
    built_in=True, which only affects the DEFAULT order (core first)
    before the user has ever saved their own order via Reorder Tabs.
    The 'after' parameter positions the tab after another tab in the default order.
    """
    _registry.register(key, built_in=built_in, label=label, build_fn=build_fn, after=after)


def ordered_tabs():
    return _registry.ordered_items()


def registry():
    return _registry
