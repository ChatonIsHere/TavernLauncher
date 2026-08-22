"""What the Mod Manager and Community Mods windows both need. The two are one
browse-and-install surface split across two Toplevels, so the pieces that must
behave identically from either - the Install action, side filtering, source
naming - live here once instead of drifting apart as two copies."""
import threading
from tkinter import messagebox

from tavern_shared.game_guard import confirm_while_game_running
from tavern_shared.mod_install import (
    _melonloader_installed, _tavernlib_installed,
)
from tavern_shared.mods.hostcfg import load_cfg
from tavern_shared.mods.install import install_mod_closure
from tavern_shared.mods.repos import _is_default, _source_display, list_repos


def meta_matches_side(meta, side):
    key = "client_side" if side == "client" else "server_side"
    return meta.get(key, True)


def source_label(source_repo):
    if _is_default(source_repo):
        return "Modding Tavern"
    return _source_display(source_repo)


def install_mod_flow(win, mod, version=None):
    """The Install action behind both windows' buttons: installs `mod`'s
    closure, at `version` if given, else mod.highest(). `win` is either
    window - both carry the _busy/_set_busy/_finish/_status machinery and the
    _game_dir/_side/_is_game_running attributes this drives.

    The cheap prerequisite checks come first, the game-running guard last,
    immediately before the worker: the guard exists for the write it precedes,
    and a player made to accept its risk for an install that was about to be
    refused anyway learns to click through it."""
    if win._busy:
        return
    if not _melonloader_installed(win._game_dir):
        messagebox.showwarning("Install MelonLoader first",
            f"{mod.name} is a MelonLoader mod. Install MelonLoader from the "
            "Setup window first.", parent=win)
        return
    if not _tavernlib_installed(win._game_dir):
        messagebox.showwarning("Install TavernLib first",
            f"{mod.name} needs TavernLib. Install it from the Setup window "
            "first.", parent=win)
        return
    if not confirm_while_game_running(win, win._is_game_running,
                                      f"Installing {mod.name}"):
        return
    label = f"{mod.name} {version}" if version else mod.name
    win._set_busy(True, f"Installing {label}...")
    index = win._index
    def worker():
        try:
            repos = list_repos(load_cfg())
            install_mod_closure(
                win._game_dir, mod, index, repos,
                lambda m: win.after(0, lambda: win._status.set(m)),
                win._side, version=version)
            win.after(0, lambda: win._finish(f"{label} installed."))
        except Exception as e:
            win.after(0, lambda e=e: win._finish(f"Install failed: {e}"))
    threading.Thread(target=worker, daemon=True).start()
