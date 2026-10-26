"""What the client and server launchers' main windows share around the game
folder: the Setup and Mod Manager buttons, the Setup alert and Mod Manager
badge behind them, and the partial-update report. Mixed into ClientLauncher
and ServerLauncher, which differ only in their mod target and the hooks below."""
import os
from tkinter import messagebox

from tavern_shared.bg import post, run_bg
from tavern_shared.mod_install import NEEDS_ATTENTION, _mods_need_attention
from tavern_shared.mods.base import load_cfg
from tavern_shared.mods.install import list_installed_mods, mod_status
from tavern_shared.mods.repos import fetch_indexes, list_repos
from tavern_shared.mods.ui import ModManagerWindow, record_matches_target
from tavern_shared.mods_window import SetupWindow
from tavern_shared.patch import _patch_needs_attention


def count_mods_needing_attention(game_dir, target):
    """How many of this launcher's installed community mods (`target`
    builds) are outdated or damaged. 0 on any failure, so a network blip never decorates the button
    with a false alarm. Hits the network and hashes files: workers only."""
    try:
        installed = [r for r in list_installed_mods(game_dir)
                     if record_matches_target(r, target)]
        if not installed:
            return 0
        index = fetch_indexes(list_repos(load_cfg()))
        return sum(1 for r in installed
                   if mod_status(game_dir, r["id"], index, target) in ("outdated", "damaged"))
    except Exception:
        return 0


class LauncherCommon:
    """Needs v_exe, _setup_btn, _mod_manager_btn and _print on the window, and
    _init_launcher_common() called before the buttons are built."""
    MOD_TARGET = "pc"           # the build this launcher installs; the server's is "server"

    def _init_launcher_common(self):
        # Read every tick by start_flashing_button; written only on the Tk thread.
        self._setup_needs_attention = False
        self._setup_win = None
        self._mod_manager_win = None
        self._exe_check_job = None
        self._alert_running = False
        self._alert_pending = None   # (rank, kwargs) of a refresh asked for mid-run

    # ── Hooks ────────────────────────────────────────────────────────────────

    def _setup_window_kwargs(self):
        return {}

    def _mod_manager_kwargs(self):
        return {}

    def _alert_probe(self, game_dir):
        """A cheap extra disk check run on the alert's worker; its result goes
        to _alert_probe_done on the Tk thread (None when there's no game)."""
        return None

    def _alert_probe_done(self, result):
        pass

    def _mods_written(self):
        """After Setup or the Mod Manager wrote into the game folder."""

    # ── Buttons and alert ────────────────────────────────────────────────────

    def _game_exe(self):
        exe = self.v_exe.get().strip()
        return exe if exe and os.path.isfile(exe) else None

    def _require_exe(self):
        exe = self._game_exe()
        if not exe:
            messagebox.showerror("Game not found",
                "Please set the path to 'A Township Tale.exe' above first.", parent=self)
        return exe

    def _on_exe_changed(self, *_):
        if self._exe_check_job:
            try: self.after_cancel(self._exe_check_job)
            except Exception: pass
        self._exe_check_job = self.after(800, self._refresh_tool_states)

    def _refresh_tool_states(self):
        """Enables/disables the Setup and Mod Manager buttons based on whether
        a valid game exe is selected, then separately refreshes the Setup
        button's flashing-alert condition. State is only ever touched here, and
        start_flashing_button only ever touches bg — kept deliberately separate
        so neither path can clobber the other."""
        state = "normal" if self._game_exe() else "disabled"
        for btn in (self._setup_btn, self._mod_manager_btn):
            try: btn.config(state=state)
            except Exception: pass
        self._refresh_setup_alert()

    def _refresh_setup_alert(self, states=None, components=True):
        """Refreshes the Setup alert (patch, MelonLoader or TavernLib missing,
        outdated or damaged), the Mod Manager badge and _alert_probe on a
        worker. states are the component states Setup just checked, used
        instead of checking again; components=False keeps the alert as it is
        and refreshes only the rest.

        One refresh runs at a time. Requests made meanwhile collapse into one
        follow-up, the most thorough of them (a full check, then states, then
        badge only)."""
        rank = 2 if components and states is None else 1 if components else 0
        if self._alert_running:
            if self._alert_pending is None or rank >= self._alert_pending[0]:
                self._alert_pending = (rank, {"states": states, "components": components})
            return
        exe = self._game_exe()
        if not exe:
            self._setup_needs_attention = False
            self._set_mod_manager_badge(0)
            self._alert_probe_done(None)
            return
        self._alert_running = True
        game_dir, target = os.path.dirname(exe), self.MOD_TARGET

        def check():
            post(self, self._alert_probe_done, self._alert_probe(game_dir))
            need = None
            if components:
                try:
                    if states:
                        need = any(s in NEEDS_ATTENTION for s in states.values())
                    else:
                        # The patch is judged by the same rule as the other
                        # two, and a failed update check never false-alarms.
                        need = _mods_need_attention(game_dir) or _patch_needs_attention(exe)
                except Exception:
                    need = False
            return need, count_mods_needing_attention(game_dir, target)

        def done(result):
            need, badge = result
            if need is not None:
                self._setup_needs_attention = need
            self._set_mod_manager_badge(badge)
            self._alert_finished()

        # Started from the event loop: the first refresh is asked for during
        # __init__, and Tkinter drops a worker's hand-back if mainloop hasn't
        # started within a second of it.
        self.after(0, lambda: run_bg(self, check, on_ok=done,
                                     on_err=lambda e: self._alert_finished()))

    def _alert_finished(self):
        self._alert_running = False
        pending, self._alert_pending = self._alert_pending, None
        if pending is not None:
            self._refresh_setup_alert(**pending[1])

    def _set_mod_manager_badge(self, count):
        label = "📦 Mod Manager"
        if count:
            label += f"  ({count} need{'s' if count == 1 else ''} attention)"
        try: self._mod_manager_btn.config(text=label)
        except Exception: pass

    # ── Windows ──────────────────────────────────────────────────────────────

    def _open_setup(self):
        exe = self._require_exe()
        if not exe:
            return
        if self._setup_win and self._setup_win.winfo_exists():
            self._setup_win.lift(); return
        self._setup_win = SetupWindow(
            self, exe, on_status_change=self._refresh_setup_alert,
            on_installed=self._on_setup_installed, **self._setup_window_kwargs())

    def _on_setup_installed(self):
        self._mods_written()
        # An open Setup re-checks and reports its states itself.
        if not (self._setup_win and self._setup_win.winfo_exists()):
            self._refresh_setup_alert()

    def _open_mod_manager(self):
        exe = self._require_exe()
        if not exe:
            return
        if self._mod_manager_win and self._mod_manager_win.winfo_exists():
            self._mod_manager_win.lift(); return
        # Browsing is harmless while the game runs, so the window opens either
        # way; the guard is on the actions that actually write.
        self._mod_manager_win = ModManagerWindow(
            self, os.path.dirname(exe), target=self.MOD_TARGET,
            on_change=self._on_mod_manager_change, **self._mod_manager_kwargs())

    def _on_mod_manager_change(self):
        self._mods_written()
        self._refresh_setup_alert(components=False)

    # ── Updates ──────────────────────────────────────────────────────────────

    def _report_partial_update(self, updater):
        """Shows the report the updater's staging process left behind when it
        couldn't replace every file (see updater.read_partial_update_marker)."""
        if updater is None:
            return
        marker = updater.read_partial_update_marker()
        if not marker:
            return
        updater.clear_partial_update_marker()
        files = marker.get("files", [])
        self._print(f"Update warning: {len(files)} file(s) could not be replaced.", "warn")
        for f in files:
            self._print(f"  · {f}", "warn")
        messagebox.showwarning("Update didn't fully apply",
                               updater.partial_update_message(files), parent=self)
