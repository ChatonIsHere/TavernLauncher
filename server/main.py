"""
Server launcher entry point. Loads whichever addons the user has
actually enabled BEFORE building the main window -- addons/custom_
models/server.py, for instance, needs ServerLauncher to already be
importable when it monkeypatches _open_custom_models onto it, but
needs to run before ServerLauncher() is ever actually instantiated.

Addons are no longer baked in at build time -- see client/main.py's
identical comment on why.
"""
import sys
import os
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _addon_log_path():
    base = os.environ.get("APPDATA", os.path.join(os.path.expanduser("~"), "AppData", "Roaming"))
    path = os.path.join(base, "TheModdingTavern")
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass
    return os.path.join(path, "addon_load_errors.log")


def _write_error_log(err_text):
    if sys.stderr is not None:
        try:
            sys.stderr.write(err_text)
        except Exception:
            pass
    try:
        with open(_addon_log_path(), "a", encoding="utf-8") as f:
            f.write(err_text + "\n")
    except Exception:
        pass


# Tell the mod manager which config file it is working against -- the server's
# own, not the client's. Same placement and reasoning as client/main.py.
from tavern_shared.mods import hostcfg as _mods_hostcfg
from server.core.data_store import load_cfg as _load_cfg, save_cfg as _save_cfg
_mods_hostcfg.set_config_accessors(_load_cfg, _save_cfg)

from server.core import addon_loader
addon_loader.load_enabled_addons("server", _write_error_log)

from server.core.launcher_window import ServerLauncher, _updater

if __name__ == "__main__":
    if _updater is not None:
        _updater.finish_update_if_requested()
        _updater.cleanup_previous_update()
    app = ServerLauncher()

    def _log_callback_exception(exc, val, tb):
        _write_error_log("".join(traceback.format_exception(exc, val, tb)))
    app.report_callback_exception = _log_callback_exception

    app.mainloop()
