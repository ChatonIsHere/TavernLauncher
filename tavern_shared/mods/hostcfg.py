"""The one piece of host state the mod manager can't just import.

Everything else it borrows from 1.8.4 -- the downloader, the hasher, the
palette, the widget factories -- is the same code for both apps, so it is a
plain import. Config isn't: client/core/config.py and server/core/data_store.py
each expose load_cfg/save_cfg over their OWN file, and which one is correct
depends on which app is running, not on which module is asking. So this stays
a wire-up.

Each app calls set_config_accessors() once at startup (client/main.py,
server/main.py) and everything below reads through here. Kept deliberately
tiny -- it is the last remnant of what used to be a broad set_helpers(host)
surface covering the downloader, the hasher and the whole palette.
"""
from tavern_shared.mods.errors import ModManagerError

_load = None
_save = None


def set_config_accessors(load, save):
    """Wire this to the running app's config. Called once at startup."""
    global _load, _save
    _load, _save = load, save


def load_cfg():
    if _load is None:
        raise ModManagerError(
            "The mod manager has no config accessor. The app must call "
            "tavern_shared.mods.hostcfg.set_config_accessors(...) at startup.")
    return _load()


def save_cfg(cfg):
    """Persist cfg, or do nothing if nothing has been wired.

    The no-op is deliberate: the logic layer is usable (and tested) with no
    host config at all, and the callers routed here are ones where losing the
    write is survivable. Callers for which it is NOT survivable ask for
    require_save_cfg() instead and get a hard error.
    """
    if _save is not None:
        _save(cfg)


def require_save_cfg():
    if _save is None:
        raise ModManagerError(
            "This action has to persist a config change, but the mod manager "
            "has no config accessor. The app must call "
            "tavern_shared.mods.hostcfg.set_config_accessors(...) at startup.")
    return _save
