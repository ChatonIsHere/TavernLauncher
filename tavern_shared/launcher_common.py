"""The Setup and Mod Manager buttons, their alert and badge, and the
partial-update report, mixed into both launchers' main windows."""
import os
from tkinter import messagebox

from tavern_shared.bg import post, run_bg
from tavern_shared.flashing import start_flashing_button
from tavern_shared.mod_install import NEEDS_ATTENTION, _mods_need_attention
from tavern_shared.mods.base import load_cfg
from tavern_shared.mods.install import list_installed_mods, mod_status
from tavern_shared.mods.repos import fetch_indexes, list_repos
from tavern_shared.mods.ui import ModManagerWindow, record_matches_target
from tavern_shared.mods_window import SetupWindow
from tavern_shared.patch import _patch_needs_attention
from tavern_shared.theme import SURF2, _btn


def count_mods_needing_attention(game_dir, target):
    """Outdated or damaged community mods; 0 on any failure. Workers only."""
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
    """Needs v_exe and _print on the window."""
    MOD_TARGET = "client_windows"
    _setup_needs_attention = False
    _setup_win = None
    _mod_manager_win = None
    _exe_check_job = None
    _alert_running = False
    _alert_pending = None   # (rank, kwargs) of a refresh asked for mid-run

    def _build_mod_buttons(self, row):
        self._setup_btn = _btn(row, "🛠 Setup", self._open_setup,
             font=("Segoe UI",9), pady=5, padx=10)
        self._setup_btn.pack(side="left")
        start_flashing_button(self._setup_btn,
            lambda: self._setup_needs_attention, normal_bg=SURF2, alert_bg="#5a3d0e")
        self._mod_manager_btn = _btn(row, "📦 Mod Manager", self._open_mod_manager,
             font=("Segoe UI",9), pady=5, padx=10)
        self._mod_manager_btn.pack(side="left", padx=(6,0))

    # ── Hooks ────────────────────────────────────────────────────────────────

    def _setup_window_kwargs(self):
        return {}

    def _mod_manager_kwargs(self):
        return {}

    def _alert_probe(self, game_dir):
        """Run on the alert's worker; the result goes to _alert_probe_done."""
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
        state = "normal" if self._game_exe() else "disabled"
        for btn in (self._setup_btn, self._mod_manager_btn):
            try: btn.config(state=state)
            except Exception: pass
        self._refresh_setup_alert()

    def _refresh_setup_alert(self, states=None, components=True):
        """Refreshes the Setup alert, the Mod Manager badge and _alert_probe on
        a worker; `states` are Setup's just-checked ones, and components=False
        leaves the alert alone. Requests during a refresh collapse into one
        follow-up, the most thorough of them."""
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

        # Via the event loop: Tkinter drops a worker's hand-back if mainloop
        # hasn't started within about a second, and the first is from __init__.
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
        # An open Setup reports its own re-check.
        if not (self._setup_win and self._setup_win.winfo_exists()):
            self._refresh_setup_alert()

    def _open_mod_manager(self):
        exe = self._require_exe()
        if not exe:
            return
        if self._mod_manager_win and self._mod_manager_win.winfo_exists():
            self._mod_manager_win.lift(); return
        self._mod_manager_win = ModManagerWindow(
            self, os.path.dirname(exe), target=self.MOD_TARGET,
            on_change=self._on_mod_manager_change, **self._mod_manager_kwargs())

    def _on_mod_manager_change(self):
        self._mods_written()
        self._refresh_setup_alert(components=False)

    # ── Updates ──────────────────────────────────────────────────────────────

    def _report_partial_update(self, updater):
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
