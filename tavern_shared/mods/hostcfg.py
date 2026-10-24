"""The one piece of host state the mod manager can't import: which app's
config file it reads and writes. client/core/config.py and
server/core/data_store.py each expose load_cfg/save_cfg over their own file,
so each app calls set_config_accessors() once at startup (client/main.py,
server/main.py) and everything here reads through it."""
from tavern_shared.mods.errors import ModManagerError

_load = None
_save = None


def set_config_accessors(load, save):
    """Wire this to the running app's config. Called once at startup."""
    global _load, _save
    _load, _save = load, save


def _unwired():
    return ModManagerError(
        "The mod manager has no config accessor. The app must call "
        "tavern_shared.mods.hostcfg.set_config_accessors(...) at startup.")


def load_cfg():
    if _load is None:
        raise _unwired()
    return _load()


def save_cfg(cfg):
    if _save is None:
        raise _unwired()
    _save(cfg)
