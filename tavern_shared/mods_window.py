"""
The Setup window -- Patch, then MelonLoader, then TavernLib, in that fixed
order. Identical between both apps (as ModsWindow before it was), so it lives
here once.

This replaced a Mods window offering MelonLoader, TavernLib and an optional
CircuitsVoiceChat. Two things changed. The patch moved in, because it is a
prerequisite like the others and having it live as its own button on the main
window meant two separate flashing alerts for one "your install isn't ready
yet" state. And CircuitsVoiceChat moved out, because it is a community mod
now: the Mod Manager installs, updates and version-pins it like any other,
which is strictly more than this window could ever do for it.
"""
import os
import threading
import tkinter as tk
from tkinter import messagebox

from tavern_shared.theme import (
    BG, SURF, BORDER, AMBER, PARCH, MUTED, GREEN,
    CYAN, _btn, _section_label,
)
from tavern_shared.window_chrome import _start_hidden, _finish_dark_window
from tavern_shared.patch import _patch_is_applied, apply_patch
from tavern_shared.mod_install import (
    _detect_exe_arch, _install_melonloader, _install_tavernlib, _load_mod_meta,
    _melonloader_installed, _melonloader_status, _tavernlib_status,
)

class SetupWindow(tk.Toplevel):
    """Patch, MelonLoader, TavernLib, in that order — the fixed sequence
    everything else in the launcher depends on. Each step's button stays
    disabled until the step before it has been installed at least once
    (see _lock_row) — a fresh install can't jump ahead — but that lock never
    blocks re-running a step that's already installed (an update or a
    reinstall), even if an earlier step has since gone missing again."""

    def __init__(self, parent, exe_path, on_status_change=None):
        super().__init__(parent)
        _start_hidden(self)
        self.title("Setup")
        self.configure(bg=BG)
        self.geometry("520x480")
        self.resizable(False, False)
        self._exe = exe_path
        self._game_dir = os.path.dirname(exe_path)
        self._busy = False
        self._on_status_change = on_status_change
        self._build()
        self.update_idletasks()
        self.geometry(f"520x{self.winfo_reqheight()}")
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        _finish_dark_window(self)

    def _on_close(self):
        if self._on_status_change: self._on_status_change()
        self.destroy()

    def _build(self):
        h = tk.Frame(self, bg=SURF, height=44)
        h.pack(fill="x"); h.pack_propagate(False)
        tk.Label(h, text="🛠  Setup", bg=SURF, fg=AMBER,
                 font=("Georgia",12,"bold")).pack(side="left", padx=16, pady=8)
        self._auto_btn = _btn(h, "⚡ Automatic Setup", self._on_automatic_setup,
                              style="primary", font=("Segoe UI",9,"bold"), pady=6, padx=12)
        self._auto_btn.pack(side="right", padx=12)
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        tk.Label(self,
            text="These set up modding for A Township Tale on this machine, in "
                 "order: Patch, then MelonLoader, then TavernLib. If GitHub can't "
                 "be reached (some networks/antivirus block it), the version "
                 "bundled with this launcher is used automatically instead.",
            bg=BG, fg=MUTED, font=("Segoe UI",9), wraplength=470, justify="left"
        ).pack(anchor="w", padx=20, pady=(10,8))

        _section_label(self, "IN ORDER")
        self._patch_btn = self._mod_row(
            "Patch", "Enables hosting and connecting to custom servers.",
            self._on_patch_click)
        self._ml_btn = self._mod_row(
            "MelonLoader", "Universal Mod Loader for Unity Games.",
            self._on_melonloader_click)
        self._tl_btn = self._mod_row(
            "TavernLib", "MelonLoader plugin to keep the game alive.",
            self._on_tavernlib_click)

        self._status = tk.StringVar(value="")
        tk.Label(self, textvariable=self._status, bg=BG, fg=CYAN,
                 font=("Segoe UI",9), wraplength=470, justify="left"
        ).pack(anchor="w", padx=20, pady=(10,10))

        self._refresh_states()

    def _mod_row(self, title, subtitle, on_click):
        row = tk.Frame(self, bg=SURF, highlightbackground=BORDER, highlightthickness=1)
        row.pack(fill="x", padx=20, pady=4)
        dotvar = tk.StringVar(value="○")
        dot = tk.Label(row, textvariable=dotvar, bg=SURF, fg=MUTED, font=("Segoe UI",13))
        dot.pack(side="left", padx=(14,10), pady=10)
        tf = tk.Frame(row, bg=SURF)
        tf.pack(side="left", fill="both", expand=True, pady=8)
        tk.Label(tf, text=title, bg=SURF, fg=PARCH, font=("Georgia",10,"bold")).pack(anchor="w")
        tk.Label(tf, text=subtitle, bg=SURF, fg=MUTED, font=("Segoe UI",8),
                 wraplength=280, justify="left").pack(anchor="w")
        # Its own line, separate from the (static) description above, so
        # "Up to date." / a version tag / a lock message doesn't get run
        # into the description text — and this line is always reserved
        # (even when empty) so a row doesn't change height when it appears.
        notevar = tk.StringVar(value="")
        note = tk.Label(tf, textvariable=notevar, bg=SURF, fg=MUTED, font=("Segoe UI",8),
                        wraplength=280, justify="left")
        note.pack(anchor="w")
        # Fixed width so the row doesn't shift when the label changes length
        # ("⬇ Install" vs "⟳ Reinstall") as a step's state changes.
        btn = _btn(row, "…", on_click, font=("Segoe UI",9), pady=6, padx=12, width=11)
        btn.pack(side="right", padx=12)
        btn._dotvar = dotvar
        btn._dotlabel = dot
        btn._notevar = notevar
        return btn

    # ── Status ───────────────────────────────────────────────────────────────

    _STATE_STYLE = {
        "missing":  ("○", MUTED, "⬇ Install"),
        "outdated": ("⚠", AMBER, "⟳ Update"),
        "unknown":  ("●", MUTED, "⟳ Reinstall"),
        "current":  ("●", GREEN, "⟳ Reinstall"),
    }
    _STATE_NOTE = {
        "missing": "",
        "outdated": "Update available.",
        "unknown": "",
        "current": "Up to date.",
    }

    def _refresh_states(self):
        self._status.set("Checking status…")
        exe, game_dir = self._exe, self._game_dir
        def worker():
            patch_state = "current" if _patch_is_applied(exe) else "missing"
            ml = _melonloader_status(game_dir)
            tl = _tavernlib_status(game_dir)
            ml_tag = _load_mod_meta(game_dir).get("melonloader_tag")
            self.after(0, lambda: self._apply_states(patch_state, ml, tl, ml_tag))
        threading.Thread(target=worker, daemon=True).start()

    def _apply_states(self, patch_state, ml_state, tl_state, ml_tag):
        self._apply_row_state(self._patch_btn, patch_state)
        self._apply_row_state(self._ml_btn, ml_state)
        self._apply_row_state(self._tl_btn, tl_state)
        # A real release tag (not the "bundled:<hash>" fallback marker) is
        # worth showing so it's obvious exactly what got installed, not just
        # that something did.
        if ml_tag and not ml_tag.startswith("bundled:"):
            note = self._ml_btn._notevar.get()
            self._ml_btn._notevar.set(f"{note}  ({ml_tag})" if note else f"({ml_tag})")
        self._lock_row(self._ml_btn, ml_state, patch_state, "Patch")
        self._lock_row(self._tl_btn, tl_state, ml_state, "MelonLoader")
        self._status.set("")
        if self._on_status_change: self._on_status_change()

    def _apply_row_state(self, btn, state):
        dot, color, text = self._STATE_STYLE[state]
        btn._dotvar.set(dot)
        btn._dotlabel.config(fg=color)
        btn.config(text=text)
        btn._notevar.set(self._STATE_NOTE[state])

    def _lock_row(self, btn, state, prior_state, prior_name):
        """Disables a step's button only when it's never been installed AND
        its prerequisite hasn't either — never blocks updating/reinstalling
        a step that's already there, no matter what the earlier step is
        doing right now."""
        locked = state == "missing" and prior_state == "missing"
        if not self._busy:
            btn.config(state="disabled" if locked else "normal")
        if locked:
            note = btn._notevar.get()
            lock_msg = f"Install {prior_name} first"
            btn._notevar.set(f"{note}  ·  {lock_msg}" if note else lock_msg)

    def _set_busy(self, busy, msg=""):
        self._busy = busy
        state = "disabled" if busy else "normal"
        self._auto_btn.config(state=state)
        self._patch_btn.config(state=state)
        self._ml_btn.config(state=state)
        self._tl_btn.config(state=state)
        self._status.set(msg)

    def _on_patch_click(self):
        if self._busy: return
        self._set_busy(True, "Checking for the latest patch…")
        exe = self._exe
        def worker():
            try:
                result = apply_patch(exe, lambda m: self.after(0, lambda: self._status.set(m)))
                messages = {
                    "downloaded": "Downloaded the latest Tavern patch from GitHub and applied it.",
                    "bundled": "Couldn't reach GitHub, so the version bundled with this "
                               "launcher was applied instead.",
                    "current": "Already up to date — no changes were needed.",
                }
                msg = messages.get(result, "Root.Township.dll has been replaced with the Tavern patch.")
                self.after(0, lambda: self._finish_install(True, msg))
            except RuntimeError as e:
                self.after(0, lambda err=str(e): self._finish_install(False, f"Patch failed: {err}"))
        threading.Thread(target=worker, daemon=True).start()

    def _on_melonloader_click(self):
        if self._busy: return
        arch = _detect_exe_arch(self._exe)
        if not arch:
            messagebox.showerror("Can't tell architecture",
                "Couldn't determine whether the game is 32- or 64-bit from "
                "the selected .exe. Try re-browsing to it on the main screen.", parent=self)
            return
        self._set_busy(True, f"Detected {arch} game — starting install…")

        def worker():
            try:
                _install_melonloader(self._game_dir, arch,
                    lambda m: self.after(0, lambda: self._status.set(m)))
                self.after(0, lambda: self._finish_install(True, "MelonLoader installed."))
            except Exception as e:
                self.after(0, lambda e=e: self._finish_install(False, f"Install failed: {e}"))
        threading.Thread(target=worker, daemon=True).start()

    def _on_tavernlib_click(self):
        if self._busy: return
        if not _melonloader_installed(self._game_dir):
            messagebox.showwarning("Install MelonLoader first",
                "TavernLib is a MelonLoader plugin — install MelonLoader above first.", parent=self)
            return
        self._set_busy(True, "Starting TavernLib install…")

        def worker():
            try:
                _install_tavernlib(self._game_dir,
                    lambda m: self.after(0, lambda: self._status.set(m)))
                self.after(0, lambda: self._finish_install(True, "TavernLib installed."))
            except Exception as e:
                self.after(0, lambda e=e: self._finish_install(False, f"Install failed: {e}"))
        threading.Thread(target=worker, daemon=True).start()

    def _on_automatic_setup(self):
        if self._busy: return
        self._set_busy(True, "Running automatic setup…")
        exe, game_dir = self._exe, self._game_dir

        def worker():
            try:
                if not _patch_is_applied(exe):
                    self.after(0, lambda: self._status.set("Applying patch…"))
                    apply_patch(exe, lambda m: self.after(0, lambda: self._status.set(m)))

                if _melonloader_status(game_dir) != "current":
                    arch = _detect_exe_arch(exe)
                    if not arch:
                        raise RuntimeError(
                            "Couldn't determine whether the game is 32- or 64-bit from "
                            "the selected .exe. Try re-browsing to it on the main screen.")
                    self.after(0, lambda: self._status.set(f"Detected {arch} game — installing MelonLoader…"))
                    _install_melonloader(game_dir, arch,
                        lambda m: self.after(0, lambda: self._status.set(m)))

                if _tavernlib_status(game_dir) != "current":
                    self.after(0, lambda: self._status.set("Installing TavernLib…"))
                    _install_tavernlib(game_dir,
                        lambda m: self.after(0, lambda: self._status.set(m)))

                self.after(0, lambda: self._finish_install(True, "Automatic setup complete."))
            except Exception as e:
                self.after(0, lambda e=e: self._finish_install(False, f"Automatic setup failed: {e}"))
        threading.Thread(target=worker, daemon=True).start()

    def _finish_install(self, ok, msg):
        self._set_busy(False, msg)
        self._refresh_states()
