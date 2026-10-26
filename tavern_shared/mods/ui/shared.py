"""What the Mod Manager and Community Mods windows both need. The two are one
browse-and-install surface split across two Toplevels, so the pieces that must
behave identically from either - the Install action, the busy/refresh cycle,
target filtering, source naming - live here once instead of drifting apart as
two copies."""
import tkinter as tk
from tkinter import messagebox

from tavern_shared.bg import post, run_bg
from tavern_shared.game_guard import confirm_while_game_running
from tavern_shared.mod_install import (
    _melonloader_installed, _tavernlib_installed,
)
from tavern_shared.mods.base import load_cfg
from tavern_shared.mods.install import (
    _is_pre_v2, has_target, install_mod_closure, mod_status,
)
from tavern_shared.mods.manifest import runs_in_scene
from tavern_shared.mods.repos import _is_default, _source_display, list_repos


def record_matches_target(rec, target):
    """Whether the launcher installing `target` lists this record: it holds
    that target, or predates manifest v2 (listed as damaged, so a reinstall
    moves it to the per-target layout)."""
    return has_target(rec, target) or _is_pre_v2(rec)


_TARGET_NAMES = {"pc": "PC", "server": "Server", "quest": "Quest"}


def target_label(targets):
    """"PC + Server", "Server only", ... for a mod's targets."""
    names = [_TARGET_NAMES.get(t, t) for t in sorted(targets, key=list(_TARGET_NAMES).index)
             if t in _TARGET_NAMES]
    if not names:
        return "-"
    return names[0] + " only" if len(names) == 1 else " + ".join(names)


def source_label(source_repo):
    if _is_default(source_repo):
        return "Modding Tavern"
    return _source_display(source_repo)


class ModListWindow(tk.Toplevel):
    """The busy/refresh cycle both mod windows share. A subclass sets _busy,
    _status, _refresh_btn, _index, _game_dir and _on_change, and provides:
      _state_ids()            ids whose mod_status to show
      _apply_states(states)   paint {id: state}
      _read_installed()       worker: what's on disk now
      _apply_installed(data)  repaint from that
      _busy_changed()         re-enable or disable per-row controls"""

    def _busy_changed(self):
        pass

    def _set_busy(self, busy, msg=""):
        self._busy = busy
        self._refresh_btn.config(state="disabled" if busy else "normal")
        if msg or not busy:
            self._status.set(msg)
        self._busy_changed()

    def _refresh_states(self):
        ids = self._state_ids()
        if not ids:
            return
        index, game_dir, target = self._index, self._game_dir, self._target
        def check():
            states = {}
            for mod_id in ids:
                try:
                    states[mod_id] = mod_status(game_dir, mod_id, index, target)
                except Exception:
                    states[mod_id] = "unknown"
            return states
        run_bg(self, check, on_ok=self._apply_states)

    def _finish(self, msg):
        """After an action that changed Mods/: re-reads what's installed (the
        index can't have changed) and tells the owner."""
        self._set_busy(False, msg)
        def reread(data):
            self._apply_installed(data)
            self._refresh_states()
        run_bg(self, self._read_installed, on_ok=reread, on_err=lambda e: None)
        if self._on_change:
            self._on_change()


def install_mod_flow(win, mod, version=None):
    """The Install action behind both windows' buttons: installs `mod`'s
    closure, at `version` if given, else mod.highest(). `win` is either
    window - both carry the ModListWindow machinery and the
    _game_dir/_target/_is_game_running attributes this drives.

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
    # The operator's call, not a refusal: they may be about to change the
    # scene. A headless server, with nobody to ask, skips such a mod instead.
    server_scene = win._server_scene() if getattr(win, "_server_scene", None) else None
    scenes = getattr(mod, "scenes", None)
    if not runs_in_scene(scenes, server_scene):
        if not messagebox.askyesno("Not made for this scene",
                f"{mod.name} says it works in the {', '.join(scenes)} "
                f"scene{'s' if len(scenes) > 1 else ''}, and this server is set "
                f"to run the {server_scene} scene.\n\nInstall it anyway?",
                parent=win):
            return
    if not confirm_while_game_running(win, win._is_game_running,
                                      f"Installing {mod.name}"):
        return
    label = f"{mod.name} {version}" if version else mod.name
    win._set_busy(True, f"Installing {label}...")
    index, game_dir, target = win._index, win._game_dir, win._target
    def install():
        install_mod_closure(game_dir, mod, index, list_repos(load_cfg()),
                            lambda m: post(win, win._status.set, m),
                            target, version=version)
    run_bg(win, install,
           on_ok=lambda _: win._finish(f"{label} installed."),
           on_err=lambda e: win._finish(f"Install failed: {e}"))
