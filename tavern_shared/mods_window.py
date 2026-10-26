"""
The Setup window -- Patch, then MelonLoader, then TavernLib, in that fixed
order. Identical between both apps, so it lives here once.
"""
import os
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from tkinter import messagebox

from tavern_shared.bg import post, run_bg
from tavern_shared.theme import (
    BG, SURF, BORDER, AMBER, PARCH, MUTED, GREEN, RED,
    CYAN, _btn, _section_label, _window_header,
)
from tavern_shared.game_guard import confirm_while_game_running
from tavern_shared.window_chrome import _start_hidden, _finish_dark_window, _set_window_icon
from tavern_shared.manual_install import ManualInstallWindow
from tavern_shared.patch import _patch_status, apply_patch
from tavern_shared.mod_install import (
    AUTO_SETUP_STATES, DownloadError, _detect_exe_arch, _install_melonloader,
    _install_tavernlib, _load_mod_meta, _melonloader_installed,
    _melonloader_status, _tavernlib_status,
)

_ARCH_UNKNOWN = ("Couldn't determine whether the game is 32- or 64-bit from "
                 "the selected .exe. Try re-browsing to it on the main screen.")


class SetupWindow(tk.Toplevel):
    """on_status_change(states) follows every check and on_installed() every
    write, reaching the owner even after this window closes."""

    def __init__(self, parent, exe_path, on_status_change=None,
                 on_installed=None, is_game_running=None):
        super().__init__(parent)
        _start_hidden(self)
        self.title("Setup")
        self.configure(bg=BG)
        self.geometry("520x480")
        self.resizable(False, False)
        _set_window_icon(self)
        self._exe = exe_path
        self._game_dir = os.path.dirname(exe_path)
        self._busy = False
        self._states = None   # {"patch"|"ml"|"tl": state} as last shown
        self._on_status_change = on_status_change
        self._on_installed = on_installed
        self._is_game_running = is_game_running
        self._build()
        self.update_idletasks()
        self.geometry(f"520x{self.winfo_reqheight()}")
        _finish_dark_window(self)

    def _build(self):
        h = _window_header(self, "🛠  Setup")
        self._auto_btn = _btn(h, "⚡ Automatic Setup", self._on_automatic_setup,
                              style="primary", font=("Segoe UI",9,"bold"), pady=6, padx=12)
        self._auto_btn.pack(side="right", padx=12)

        tk.Label(self,
            text="These set up modding for A Township Tale on this machine, in "
                 "order: Patch, then MelonLoader, then TavernLib. Each is "
                 "downloaded fresh from its official GitHub release; if a "
                 "download keeps failing (some networks/antivirus block it), "
                 "you'll get step-by-step instructions to do it in your browser "
                 "instead.",
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

        # Only shown once the automatic path has actually failed.
        self._manual_bar = tk.Frame(self, bg=BG)
        self._manual_btn = _btn(self._manual_bar, "📄 Manual Install…",
                                self._open_manual, font=("Segoe UI",9), pady=6, padx=12)
        self._manual_btn.pack(side="left")
        self._manual_for = None

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
        notevar = tk.StringVar(value="")
        note = tk.Label(tf, textvariable=notevar, bg=SURF, fg=MUTED, font=("Segoe UI",8),
                        wraplength=280, justify="left")
        note.pack(anchor="w")
        btn = _btn(row, "…", on_click, font=("Segoe UI",9), pady=6, padx=12, width=11)
        btn.pack(side="right", padx=12)
        btn._dotvar = dotvar
        btn._dotlabel = dot
        btn._notevar = notevar
        btn._notelabel = note
        return btn

    # ── Status ───────────────────────────────────────────────────────────────

    _STATE_STYLE = {
        "missing":    ("○", MUTED, "⬇ Install"),
        "outdated":   ("⚠", AMBER, "⟳ Update"),
        "damaged":    ("✕", RED,   "⟳ Repair"),
        "unrecorded": ("●", AMBER, "⟳ Reinstall"),
        "unknown":    ("●", MUTED, "⟳ Reinstall"),
        "current":    ("●", GREEN, "⟳ Reinstall"),
    }
    _STATE_NOTE = {
        "missing":    ("Not installed yet.", MUTED),
        "outdated":   ("Update available — a newer version is published.", AMBER),
        "damaged":    ("Installed file doesn't match what was installed — repair it.", RED),
        "unrecorded": ("Installed, but its install record is incomplete — "
                       "reinstall (or run Automatic Setup) to enable update checks.", AMBER),
        "unknown":    ("Installed — couldn't check for updates.", MUTED),
        "current":    ("Up to date.", MUTED),
    }

    # Install-record keys per row: (release tag, install source).
    _META_KEYS = {"patch": ("patch_tag", "patch_source"),
                  "ml":    ("melonloader_tag", "melonloader_source"),
                  "tl":    ("tavernlib_tag", "tavernlib_source")}

    def _refresh_states(self, keep_status=None):
        """keep_status, a finished install's message, stays on screen: on a
        failure it's the only place the reason is shown."""
        self._status.set("Checking status…" if keep_status is None else keep_status)
        exe, game_dir = self._exe, self._game_dir
        owner, notify = self.master, self._on_status_change
        def check():
            # Each check makes its own GitHub lookup, so they run side by side.
            with ThreadPoolExecutor(3) as pool:
                futures = {"patch": pool.submit(_patch_status, exe),
                           "ml":    pool.submit(_melonloader_status, game_dir),
                           "tl":    pool.submit(_tavernlib_status, game_dir)}
                states = {k: f.result() for k, f in futures.items()}
            if notify:
                post(owner, notify, dict(states))
            return states, _load_mod_meta(game_dir)
        run_bg(self, check,
               on_ok=lambda r: self._apply_states(*r, keep_status=keep_status),
               on_err=lambda e: self._status.set(f"Couldn't check what's installed: {e}"))

    def _apply_states(self, states, meta, keep_status=None):
        self._states = states
        for key, btn in (("patch", self._patch_btn), ("ml", self._ml_btn),
                         ("tl", self._tl_btn)):
            tag_key, source_key = self._META_KEYS[key]
            self._apply_row_state(btn, states[key], meta.get(tag_key),
                                  meta.get(source_key) == "bundled")
        self._lock_row(self._ml_btn, states["ml"], states["patch"], "Patch")
        self._lock_row(self._tl_btn, states["tl"], states["ml"], "MelonLoader")
        self._status.set("" if keep_status is None else keep_status)

    def _apply_row_state(self, btn, state, tag=None, offline=False):
        dot, color, text = self._STATE_STYLE[state]
        btn._dotvar.set(dot)
        btn._dotlabel.config(fg=color)
        btn.config(text=text)
        note, note_color = self._STATE_NOTE[state]
        # Upstream's "bundled:<hash>" markers name no release.
        if tag and not tag.startswith("bundled:") and state != "missing":
            shown = f"{tag} · offline copy" if offline else tag
            note = f"{note}  ({shown})" if note else f"({shown})"
        btn._notevar.set(note)
        btn._notelabel.config(fg=note_color)

    def _lock_row(self, btn, state, prior_state, prior_name):
        """A fresh install can't skip ahead, but an installed step can always
        be re-run."""
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
        for btn in (self._auto_btn, self._patch_btn, self._ml_btn, self._tl_btn):
            btn.config(state=state)
        self._status.set(msg)

    def _run_install(self, busy_msg, work, fail_prefix, stage):
        """work(progress) returns (message, wrote something); stage[0] names
        the component a failure is about."""
        self._set_busy(True, busy_msg)
        progress = lambda m: post(self, self._status.set, m)
        owner, on_installed = self.master, self._on_installed
        def run():
            msg, changed = work(progress)
            if changed and on_installed:
                post(owner, on_installed)
            return msg
        run_bg(self, run,
               on_ok=lambda msg: self._finish_install(True, msg),
               on_err=lambda e: self._finish_install(
                   False, f"{fail_prefix}: {e}", stage[0],
                   auto_manual=isinstance(e, DownloadError)))

    def _on_patch_click(self):
        if self._busy: return
        if not confirm_while_game_running(self, self._is_game_running,
                                          "Replacing the game's patched assembly"):
            return
        exe = self._exe
        def work(progress):
            if apply_patch(exe, progress) == "current":
                return "Already up to date — no changes were needed.", False
            return "Downloaded the latest Tavern patch from GitHub and applied it.", True
        self._run_install("Checking for the latest patch…", work, "Patch failed", ["Patch"])

    def _on_melonloader_click(self):
        if self._busy: return
        if not confirm_while_game_running(self, self._is_game_running,
                                          "Installing MelonLoader"):
            return
        arch = _detect_exe_arch(self._exe)
        if not arch:
            messagebox.showerror("Can't tell architecture", _ARCH_UNKNOWN, parent=self)
            return
        game_dir = self._game_dir
        def work(progress):
            _install_melonloader(game_dir, arch, progress)
            return "MelonLoader installed.", True
        self._run_install(f"Detected {arch} game — starting install…", work,
                          "Install failed", ["MelonLoader"])

    def _on_tavernlib_click(self):
        if self._busy: return
        if not confirm_while_game_running(self, self._is_game_running,
                                          "Installing TavernLib"):
            return
        if not _melonloader_installed(self._game_dir):
            messagebox.showwarning("Install MelonLoader first",
                "TavernLib is a MelonLoader plugin — install MelonLoader above first.", parent=self)
            return
        game_dir = self._game_dir
        def work(progress):
            _install_tavernlib(game_dir, progress)
            return "TavernLib installed.", True
        self._run_install("Starting TavernLib install…", work, "Install failed", ["TavernLib"])

    def _on_automatic_setup(self):
        if self._busy: return
        if not confirm_while_game_running(self, self._is_game_running,
                                          "Running setup"):
            return
        exe, game_dir = self._exe, self._game_dir
        # Only a click that beat the first check has to check for itself.
        shown = dict(self._states or {})
        stage = ["Patch"]

        def work(progress):
            changed = False
            if (shown.get("patch") or _patch_status(exe)) in AUTO_SETUP_STATES:
                progress("Applying patch…")
                changed = apply_patch(exe, progress) != "current"

            stage[0] = "MelonLoader"
            if (shown.get("ml") or _melonloader_status(game_dir)) in AUTO_SETUP_STATES:
                arch = _detect_exe_arch(exe)
                if not arch:
                    raise RuntimeError(_ARCH_UNKNOWN)
                progress(f"Detected {arch} game — installing MelonLoader…")
                _install_melonloader(game_dir, arch, progress)
                changed = True

            stage[0] = "TavernLib"
            if (shown.get("tl") or _tavernlib_status(game_dir)) in AUTO_SETUP_STATES:
                progress("Installing TavernLib…")
                _install_tavernlib(game_dir, progress)
                changed = True
            return "Automatic setup complete.", changed
        self._run_install("Running automatic setup…", work,
                          "Automatic setup failed", stage)

    def _finish_install(self, ok, msg, component=None, auto_manual=False):
        self._set_busy(False, msg)
        if ok:
            self._hide_manual_offer()
        elif component:
            self._show_manual_offer(component, msg)
            if auto_manual:
                self._open_manual()
        self._refresh_states(keep_status=msg)

    # ── Manual install fallback ──────────────────────────────────────────────

    def _show_manual_offer(self, component, reason):
        self._manual_for = (component, reason)
        self._manual_btn.config(text=f"📄 Install {component} manually…")
        if not self._manual_bar.winfo_ismapped():
            self._manual_bar.pack(fill="x", padx=20, pady=(0, 10))
            self.update_idletasks()
            self.geometry(f"520x{self.winfo_reqheight()}")

    def _hide_manual_offer(self):
        self._manual_for = None
        if self._manual_bar.winfo_ismapped():
            self._manual_bar.pack_forget()
            self.update_idletasks()
            self.geometry(f"520x{self.winfo_reqheight()}")

    def _open_manual(self):
        if not self._manual_for:
            return
        component, reason = self._manual_for
        ManualInstallWindow(self, self._exe, component, reason=reason,
                            on_recheck=self._refresh_states)
