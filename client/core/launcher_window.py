"""
The main Client launcher window -- username/platform/destination fields,
Mods/Patch buttons, Check/Join Server, and the game log panel. Custom
asset syncing is NOT hardcoded here anymore: _do_launch calls
run_post_auth_hooks() unconditionally, and addons/custom_models/client.py
is what actually does anything at that point, if it's part of this build.
"""
import os
import sys
import time
import json
import socket
import subprocess
import threading
import webbrowser
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
from urllib.parse import urlencode
import urllib.request

from tavern_shared.theme import (
    BG, SURF, SURF2, BORDER, AMBER, AMBERDIM, PARCH, MUTED, GREEN, RED, CYAN, MONO,
    _btn, _field, _hint, _section_label, _divider, _mk_combobox, _mk_scrollbar,
)
from tavern_shared.window_chrome import _start_hidden, _finish_dark_window, _set_window_icon, _header_crop_box
from tavern_shared.assets import _HEADER_BANNER_IMG
try:
    from PIL import Image as _PILImage, ImageTk as _PILImageTk, ImageEnhance as _PILImageEnhance
except ImportError:
    pass
from tavern_shared.log_tailer import GameLogTailer
from tavern_shared.mod_install import (
    _melonloader_installed, _mods_need_attention, _tavernlib_installed,
)
from tavern_shared.patch import _patch_source_path, _patch_is_applied
from tavern_shared.paths import _delete_last_rejection_file, _last_rejection_path
from tavern_shared.mods_window import SetupWindow
from tavern_shared import mods
from tavern_shared.mods.ui.manager_window import ModManagerWindow
from client.core.mod_diff_window import ModDiffWindow

from client.core.config import (
    load_cfg, save_cfg, CONFIG_FILE, GAME_LOG_PATH, COMMUNITY_API, DISCORD_URL,
    PLATFORM_DISPLAY_TO_BACKEND, PLATFORM_LEGACY_TO_DISPLAY,
    USERNAME_MAX_LEN, _is_valid_username, _any_token_files_exist, _get_or_create_token,
    _known_ticket_hosts, _has_unseen_owner_reply,
)
from tavern_shared.flashing import start_flashing_button
from client.core.auth import (
    AUTH_PORT, authenticate, ping_server, _resolve_ip_for_game, _valid_port,
    build_tokens, _headless_user_id, register_whitelist_application, ticket_request,
    fetch_server_mods,
)
from client.core.launch_hooks import run_post_auth_hooks
from client.core.community_browser import CommunityBrowser
from client.core.server_list_panel import ServerListPanel
from client.core.tickets_window import TicketsWindow
from client.core.remote_console import RemoteConsoleWindow
from client.core.tavernkeeper.window import TavernKeeperWindow
from client.core.game_settings_window import GameSettingsWindow
from client.version import APP_VERSION, UPDATE_APP_FOLDER

try:
    import updater as _updater
except ImportError:
    _updater = None

class ClientLauncher(tk.Tk):
    def __init__(self):
        super().__init__()
        _start_hidden(self)
        self.title("TavernLauncher - Client")
        self.configure(bg=BG)
        # This is the one window players stare at while the log scrolls —
        # letting it resize means the log area actually gets to use whatever
        # space is available instead of being locked to one fixed height.
        self.resizable(True, True)
        self.geometry("540x820")  # placeholder; resized to fit content below
        _set_window_icon(self)
        # Guarded rather than called unconditionally -- ttk.Style() is a
        # single global object shared across the whole app, and
        # unconditionally re-invoking theme_use() every time any window
        # opens (even redundantly re-setting the SAME theme) can reset
        # custom style configurations already applied elsewhere (e.g. a
        # combobox styled by an already-open window), which is almost
        # certainly why collapsed comboboxes intermittently reverted to
        # plain white once more than one window was involved.
        if ttk.Style().theme_use() != "clam":
            ttk.Style().theme_use("clam")
        self._tailer      = None
        self._server_ok   = False   # True once Check Server succeeds
        self._checked_host = None
        # Tracks whether the currently-filled-in server (from the Community
        # browser) is an official Tavern server or a headless/direct-connect
        # one — controls whether Join Server goes through the auth handshake
        # at all. Manually typed IPs and Saved/Recent selections always reset
        # this back to "official", matching how they've always behaved.
        self._selected_kind = "official"
        # Read every tick by start_flashing_button on the main thread, and only
        # ever written from the main thread (see _refresh_setup_alert), so the
        # background check behind it can't tear it.
        self._setup_needs_attention = False
        self._exe_check_job   = None
        # Join-time mod reconciliation cache: host -> (mods_hash, mods_list),
        # so rejoining a server whose mods haven't changed since last time
        # skips the extra "mods_list" round trip entirely (see _get_server_mods).
        self._mods_list_cache = {}
        self._mod_manager_win = None
        self._build_ui()
        self._load()
        # Start at exactly the size the fully-built layout needs, then set
        # that as the floor — shrinking further would start cutting into
        # either the log area or the bottom toggle row (whichever runs out
        # of room first), while growing beyond it just gives the log more
        # room to breathe. fit_w used to be a hardcoded guess that went
        # stale every time a row gained another button/checkbox — measuring
        # it the same way fit_h already was is what actually keeps this
        # correct going forward.
        self.update_idletasks()
        fit_w = max(540, self.winfo_reqwidth())
        fit_h = self.winfo_reqheight()
        self.geometry(f"{fit_w}x{fit_h}")
        self.minsize(fit_w, fit_h)
        _finish_dark_window(self)

    # ── UI ─────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        self._header()

        _section_label(self, "Path to 'A Township Tale.exe'")
        pf = _field(self)
        self.v_exe = tk.StringVar()
        self.v_exe.trace_add("write", self._on_exe_changed)
        tk.Entry(pf, textvariable=self.v_exe, bg=SURF, fg=PARCH,
                 insertbackground=AMBER, relief="flat", font=("Consolas",10),
                 bd=6).pack(side="left", fill="x", expand=True)
        _btn(pf, "Browse", self._browse, font=("Segoe UI",9),
             padx=10, pady=6).pack(side="right")
        btn_row_mods = tk.Frame(self, bg=BG)
        btn_row_mods.pack(fill="x", padx=20, pady=(4,0))
        self._setup_btn = _btn(btn_row_mods, "🛠 Setup", self._open_setup,
             font=("Segoe UI",9), pady=5, padx=10)
        self._setup_btn.pack(side="left")
        start_flashing_button(self._setup_btn,
            lambda: self._setup_needs_attention, normal_bg=SURF2, alert_bg="#5a3d0e")
        self._mod_manager_btn = _btn(btn_row_mods, "📦 Mod Manager",
             self._open_mod_manager, font=("Segoe UI",9), pady=5, padx=10)
        self._mod_manager_btn.pack(side="left", padx=(6,0))
        _btn(btn_row_mods, "🎮 Game Settings", self._open_game_settings,
             font=("Segoe UI",9), pady=5, padx=10).pack(side="right")
        _hint(self, "Run Setup before you launch the game, in order — "
                    "Patch, then MelonLoader, then TavernLib")

        _divider(self)

        _section_label(self, "CHOOSE YOUR USERNAME")
        nf = _field(self)
        self.v_username = tk.StringVar()
        tk.Entry(nf, textvariable=self.v_username, bg=SURF, fg=PARCH,
                 insertbackground=AMBER, relief="flat", font=("Consolas",10),
                 bd=6).pack(fill="x")

        _section_label(self, "CHOOSE YOUR PLATFORM")
        pf2 = tk.Frame(self, bg=SURF, highlightbackground=BORDER, highlightthickness=1)
        pf2.pack(fill="x", padx=20, pady=(0,4))
        self.v_platform = tk.StringVar(value="SteamVR")
        _mk_combobox(pf2, self.v_platform, ["SteamVR","Quest","Fly"])

        _divider(self)

        _section_label(self, "Destination (leave blank for localhost)")
        sf = _field(self)
        self.v_ip = tk.StringVar()
        self.v_ip.trace_add("write", self._on_ip_changed)
        tk.Entry(sf, textvariable=self.v_ip, bg=SURF, fg=PARCH,
                 insertbackground=AMBER, relief="flat", font=("Consolas",10),
                 bd=6).pack(side="left", fill="x", expand=True)
        self.v_port = tk.StringVar(value="1757")
        tk.Entry(sf, textvariable=self.v_port, bg=SURF, fg=PARCH,
                 insertbackground=AMBER, relief="flat", font=("Consolas",10),
                 bd=6, width=6, justify="center").pack(side="left", padx=(4,0))

        btn_row_dest = tk.Frame(self, bg=BG)
        btn_row_dest.pack(fill="x", padx=20, pady=(4,0))
        _btn(btn_row_dest, "⚑ Saved",             self._open_server_list,
             font=("Segoe UI",9), pady=5, padx=10).pack(side="left")
        _btn(btn_row_dest, "🌍 Community Servers", self._open_community,
             font=("Segoe UI",9), pady=5, padx=10).pack(side="left", padx=6)
        self._tickets_btn = _btn(btn_row_dest, "🎫 Tickets",           self._open_tickets,
             font=("Segoe UI",9), pady=5, padx=10)
        self._tickets_btn.pack(side="left", padx=(0,6))
        self._has_unseen_ticket_reply = False
        start_flashing_button(self._tickets_btn,
            lambda: self._has_unseen_ticket_reply, normal_bg=SURF2, alert_bg=RED)
        threading.Thread(target=self._poll_ticket_replies_loop, daemon=True).start()
        _btn(btn_row_dest, "🖥 Remote Console",    self._open_remote_console,
             font=("Segoe UI",9), pady=5, padx=10).pack(side="left", padx=(0,6))
        _btn(btn_row_dest, "🧩 TavernKeeper",     self._open_tavernkeeper,
             font=("Segoe UI",9), pady=5, padx=10).pack(side="left")

        # ── Action area ──────────────────────────────────────────────────────
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x", padx=20, pady=6)

        # Status line shown after Check Server — plain label, not a box, with
        # the mod-sync button beside it. The button is packed only once a check
        # comes back from a server that speaks the mod protocol (see _check_ok),
        # so it never shows for a server that has nothing to sync against.
        check_row = tk.Frame(self, bg=BG)
        check_row.pack(fill="x", padx=22, pady=(0,4))
        self._check_status = tk.StringVar(value="")
        self._check_label = tk.Label(check_row, textvariable=self._check_status,
                 bg=BG, fg=MUTED, font=("Segoe UI",9),
                 justify="left", anchor="w", wraplength=420)
        self._check_label.pack(side="left")
        self._sync_btn = _btn(check_row, "⇄ Sync Mods", self._on_sync_mods,
                              font=("Segoe UI",9), pady=4, padx=10)

        # Check Server and Join Server sit side by side — checking is purely
        # optional/informational now, never a gate on joining.
        action_row = tk.Frame(self, bg=BG)
        action_row.pack(fill="x", padx=20, pady=(0,4))
        self._check_btn = _btn(action_row, "🔍  Check Server", self._do_check,
                                font=("Georgia",12,"bold"), pady=14)
        self._check_btn.pack(side="left", fill="x", expand=True, padx=(0,4))
        self._action_btn = _btn(action_row, "⚔  Join Server", self._on_join_clicked,
                                style="primary", font=("Georgia",12,"bold"), pady=14)
        self._action_btn.pack(side="left", fill="x", expand=True, padx=(4,0))

        # ── Log ───────────────────────────────────────────────────────────────
        _section_label(self, "GAME LOG")
        lf = tk.Frame(self, bg=BG)
        lf.pack(fill="both", expand=True, padx=20, pady=(0,8))
        lb = tk.Frame(lf, bg=SURF, highlightbackground=BORDER, highlightthickness=1)
        lb.pack(fill="both", expand=True)
        self.log = tk.Text(lb, bg=SURF, fg="#b09a78", font=MONO,
                           relief="flat", bd=0, state="disabled", height=12,
                           wrap="none")
        sb = _mk_scrollbar(lb, self.log.yview)
        sb.pack(side="right", fill="y")
        self.log.config(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        for t,c in [("ok",GREEN),("warn",AMBER),("err",RED),
                    ("cyan",CYAN),("dim",MUTED),("error",RED),
                    ("info","#b09a78"),("debug",MUTED)]:
            self.log.tag_config(t, foreground=c)

        # ── Enhanced Debugging / Show MelonLoader toggles ────────────────────
        df = tk.Frame(self, bg=BG)
        df.pack(side="bottom", fill="x", padx=14, pady=(0,6))
        self.v_debug_helper = tk.BooleanVar(value=False)
        tk.Checkbutton(df, text="Enhanced Debugging", variable=self.v_debug_helper,
                       command=self._save, bg=BG, fg=MUTED, selectcolor=SURF,
                       activebackground=BG, activeforeground=AMBER,
                       padx=0, pady=0, borderwidth=0, highlightthickness=0,
                       font=("Segoe UI",7)).pack(side="left")
        self.v_show_melonloader = tk.BooleanVar(value=False)
        tk.Checkbutton(df, text="Show MelonLoader", variable=self.v_show_melonloader,
                       command=self._save, bg=BG, fg=MUTED, selectcolor=SURF,
                       activebackground=BG, activeforeground=AMBER,
                       padx=0, pady=0, borderwidth=0, highlightthickness=0,
                       font=("Segoe UI",7)).pack(side="left", padx=(8,0))
        _btn(df, "🗑 Wipe Cache", self._wipe_cache,
             font=("Segoe UI",7), pady=2, padx=6).pack(side="right")
        self._addons_btn = _btn(df, "🧩 Addons", self._open_addons,
             font=("Segoe UI",7), pady=2, padx=6)
        self._addons_btn.pack(side="right", padx=(0,6))

    def _header(self):
        h = tk.Frame(self, bg=SURF, height=64)
        h.pack(fill="x"); h.pack_propagate(False)

        canvas = tk.Canvas(h, bg=SURF, highlightthickness=0, bd=0)
        canvas.pack(fill="both", expand=True)
        self._header_canvas   = canvas
        self._header_bg_photo = None
        self._header_bg_item  = None
        if _HEADER_BANNER_IMG is not None:
            self._header_bg_item = canvas.create_image(0, 0, anchor="nw")

        canvas.create_rectangle(0, 0, 4, 64, fill=AMBER, width=0)
        canvas.create_text(18, 32, text="⚔", fill=AMBER, font=("Georgia",22), anchor="w")
        canvas.create_text(66, 21, text="The Modding Tavern", fill=AMBER,
                           font=("Georgia",14,"bold"), anchor="w")
        canvas.create_text(66, 42, text=f"Client Launcher  ·  v{APP_VERSION}", fill=AMBER,
                           font=("Segoe UI",9), anchor="w")

        self._discord_btn = tk.Button(canvas, text="💬 Discord", bg=SURF2, fg=AMBER,
                                      activebackground=AMBERDIM, activeforeground="#ffd080",
                                      relief="flat", bd=0, cursor="hand2",
                                      font=("Segoe UI",9,"bold"), padx=10, pady=4,
                                      command=lambda: webbrowser.open(DISCORD_URL))
        self._discord_btn_item = canvas.create_window(0, 32, anchor="e", window=self._discord_btn)

        # Token badge — a real Button (for its existing click/animation
        # logic) embedded onto the canvas so it layers correctly over the
        # banner image; created hidden, _show_token_button() reveals it.
        self._token_note = (
            "A token file has been created for you. This file is used to prove who you "
            "are when connecting to a server with your chosen username. It can be found "
            "in your %AppData%\\Roaming\\TheModdingTavern\\tokens folder. Make sure to keep "
            "this file safe, as you won't be able to connect with this account if it is "
            "lost. If you do lose it - please reach out to the server owner to get it back."
        )
        self._token_animating = False
        self._token_anim_job  = None
        self._token_anim_phase = 0
        self._token_btn = tk.Button(canvas, text="🔑 Token", bg=SURF2, fg=AMBER,
                                    activebackground=AMBERDIM, activeforeground="#ffd080",
                                    relief="flat", bd=0, cursor="hand2",
                                    font=("Segoe UI",9,"bold"), padx=10, pady=4,
                                    command=self._on_token_button_click)
        self._token_btn_item = canvas.create_window(0, 32, anchor="e",
                                                     window=self._token_btn, state="hidden")

        canvas.bind("<Configure>", self._on_header_resize)
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

    def _on_header_resize(self, event):
        """Rescales the banner to fill the header exactly, and keeps the
        Discord/token badges right-aligned — none of this reflows on its
        own, since a Canvas doesn't auto-stretch or reposition children."""
        w, hgt = event.width, event.height
        if w < 2 or hgt < 2:
            return
        if _HEADER_BANNER_IMG is not None and self._header_bg_item is not None:
            try:
                box = _header_crop_box(_HEADER_BANNER_IMG.width, _HEADER_BANNER_IMG.height, w, hgt)
                resized = _HEADER_BANNER_IMG.crop(box).resize((w, hgt), _PILImage.LANCZOS)
                # Uniform darken so the amber/parchment text stays legible
                # regardless of which part of the artwork ends up behind it.
                resized = _PILImageEnhance.Brightness(resized).enhance(0.5)
                photo = _PILImageTk.PhotoImage(resized)
                self._header_canvas.itemconfig(self._header_bg_item, image=photo)
                self._header_bg_photo = photo  # keep a reference or Tk drops it
            except Exception:
                pass
        self._header_canvas.coords(self._discord_btn_item, w - 14, hgt // 2)
        discord_w = self._discord_btn.winfo_reqwidth()
        self._header_canvas.coords(self._token_btn_item, w - 14 - discord_w - 10, hgt // 2)

    # ── Token badge / animation ─────────────────────────────────────────────

    def _show_token_button(self):
        """Reveal the token badge. Called at startup if a token file already
        exists, and after every successful connection. Only starts the
        flash if the player hasn't already clicked through the "Yes, I
        understand" acknowledgment — once they have, it stays a plain,
        non-flashing button for the rest of time, on this machine."""
        if self._header_canvas.itemcget(self._token_btn_item, "state") != "normal":
            self._header_canvas.itemconfigure(self._token_btn_item, state="normal")
            # Position it correctly immediately — otherwise it sits at the
            # placeholder (0, ...) coordinate from creation until the next
            # window resize happens to trigger a reposition. Same formula as
            # _on_header_resize: left of the always-visible Discord button.
            w   = self._header_canvas.winfo_width()
            hgt = self._header_canvas.winfo_height()
            discord_w = self._discord_btn.winfo_reqwidth()
            self._header_canvas.coords(self._token_btn_item, w - 14 - discord_w - 10, hgt // 2)
        if not self._token_animating and not load_cfg().get("token_ack", False):
            self._start_token_animation()

    def _start_token_animation(self):
        self._token_animating = True
        self._token_anim_phase = 0
        self._animate_token_btn()

    def _stop_token_animation(self):
        self._token_animating = False
        if self._token_anim_job:
            try: self.after_cancel(self._token_anim_job)
            except Exception: pass
            self._token_anim_job = None
        try: self._token_btn.config(bg=SURF2, fg=AMBER)
        except Exception: pass

    def _animate_token_btn(self):
        if not self._token_animating: return
        bg, fg = (SURF2, AMBER) if self._token_anim_phase % 2 == 0 else ("#5a3d0e", "#ffd080")
        try: self._token_btn.config(bg=bg, fg=fg)
        except Exception: return
        self._token_anim_phase += 1
        self._token_anim_job = self.after(450, self._animate_token_btn)

    def _on_token_button_click(self):
        win = tk.Toplevel(self)
        _start_hidden(win)
        win.title("About Your Token File")
        win.configure(bg=BG)
        win.resizable(False, False)
        _set_window_icon(win)
        tk.Label(win, text=self._token_note, bg=BG, fg=PARCH, justify="left",
                 wraplength=360, font=("Segoe UI",9)).pack(padx=20, pady=(20,16))

        def _ack():
            cfg = load_cfg()
            cfg["token_ack"] = True
            save_cfg(cfg)
            self._stop_token_animation()
            win.destroy()

        _btn(win, "Yes, I understand", _ack, "primary",
             font=("Segoe UI",10,"bold"), pady=10).pack(fill="x", padx=20, pady=(0,20))
        win.update_idletasks()
        win.geometry(f"400x{win.winfo_reqheight()}")
        _finish_dark_window(win)
        win.transient(self)
        win.grab_set()
        # Block until it's dismissed. The first-token case opens this from the
        # middle of a join, and the mod comparison follows right after; without
        # the wait both windows appear at once and the comparison's own grab
        # takes the input, leaving the token note unreadable and unclickable.
        win.wait_window()

    # ── Setup alert ─────────────────────────────────────────────────────────
    # Unlike the token badge, this flashes only *while there's a problem* —
    # a mod missing or out of date — and stops on its own once resolved.

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
        exe = self.v_exe.get().strip()
        valid = bool(exe and os.path.isfile(exe))
        state = "normal" if valid else "disabled"
        try: self._setup_btn.config(state=state)
        except Exception: pass
        try: self._mod_manager_btn.config(state=state)
        except Exception: pass
        self._refresh_setup_alert()

    def _refresh_setup_alert(self):
        """One alert for the whole Setup sequence: a required mod missing or
        outdated, or the patch present but not applied. These used to be two
        separately flashing buttons for what is really one "this install isn't
        ready yet" state.

        The checks hit the network and the disk, so they run on a worker and
        only the resulting flag is written back on the main thread -- the
        flashing loop reads it every tick and must never block."""
        exe = self.v_exe.get().strip()
        if not exe or not os.path.isfile(exe):
            self._setup_needs_attention = False
            return
        game_dir = os.path.dirname(exe)
        def worker():
            try:
                need = _mods_need_attention(game_dir) or (
                    os.path.isfile(_patch_source_path()) and not _patch_is_applied(exe))
            except Exception:
                need = False
            self.after(0, lambda: setattr(self, "_setup_needs_attention", need))
        threading.Thread(target=worker, daemon=True).start()

    # ── Persistence ────────────────────────────────────────────────────────────

    def _load(self):
        cfg = load_cfg()
        self.v_exe.set(cfg.get("game_exe",""))
        self.v_username.set(cfg.get("username",""))
        # "none" (flatscreen) is temporarily disabled — exploitable — so a
        # value saved before this change doesn't silently keep working just
        # because it's already sitting in the user's config file. Also
        # translates a pre-rename save ("OpenVR"/"Oculus") to the current
        # display names, so upgrading doesn't silently reset this choice.
        saved_platform = cfg.get("platform", "SteamVR")
        saved_platform = PLATFORM_LEGACY_TO_DISPLAY.get(saved_platform, saved_platform)
        self.v_platform.set(saved_platform if saved_platform in ("SteamVR", "Quest", "Fly") else "SteamVR")
        self.v_ip.set(cfg.get("last_ip",""))
        self.v_port.set(cfg.get("last_port","1757"))
        self.v_debug_helper.set(cfg.get("debug_helper", False))
        self.v_show_melonloader.set(cfg.get("show_melonloader", False))
        self._print("Ready. Enter a server IP, then Check Server (optional) or Join Server.", "dim")
        self._start_log_tailer()
        if _any_token_files_exist():
            self._show_token_button()
        # Immediate check at startup — the trace-driven debounce from
        # v_exe.set above will also fire, but 800ms later; this makes the
        # Patch/Mods button states correct from the very first frame.
        self._refresh_tool_states()
        # Update check runs a couple seconds after startup, off the UI
        # thread, so it never delays the window actually appearing.
        self.after(2000, self._check_for_launcher_update)

    def _check_for_launcher_update(self):
        if _updater is None:
            return
        def worker():
            result = _updater.check_for_update(APP_VERSION, UPDATE_APP_FOLDER)
            if result:
                tag, url = result
                self.after(0, lambda: self._prompt_launcher_update(tag, url))
        threading.Thread(target=worker, daemon=True).start()

    def _prompt_launcher_update(self, tag, url):
        if not messagebox.askyesno("Update Available",
                f"A new version is available: {tag} (you have {APP_VERSION}).\n\n"
                "Update now? The launcher will restart automatically.", parent=self):
            return
        self._print(f"Updating to {tag}…", "warn")
        def worker():
            try:
                _updater.download_and_apply_update(url, UPDATE_APP_FOLDER,
                    on_progress=lambda m: self.after(0, lambda: self._print(m, "warn")))
                # download_and_apply_update relaunches and calls os._exit()
                # on success — if we get here at all, something went wrong
                # after the point of no return.
            except Exception as e:
                self.after(0, lambda: messagebox.showerror("Update failed",
                    f"Couldn't apply the update:\n{e}\n\n"
                    "The current version is unaffected — nothing was replaced.", parent=self))
        threading.Thread(target=worker, daemon=True).start()

    def _save(self):
        cfg = load_cfg()
        cfg.update({"game_exe": self.v_exe.get(), "username": self.v_username.get(),
                    "platform": self.v_platform.get(), "last_ip": self.v_ip.get(),
                    "last_port": self.v_port.get(),
                    "debug_helper": self.v_debug_helper.get(),
                    "show_melonloader": self.v_show_melonloader.get()})
        save_cfg(cfg)

    def _wipe_cache(self):
        if not messagebox.askyesno("Wipe Launcher Cache",
                "This will delete this launcher's saved settings file:\n\n"
                f"{CONFIG_FILE}\n\n"
                "That includes your saved username, game path, last server "
                "IP, and toggle preferences — giving you a completely fresh, "
                "unconfigured launcher next time it starts.\n\n"
                "Your token files, patch, and installed mods are NOT affected.\n\n"
                "This cannot be undone. Continue?", icon="warning", parent=self):
            return
        try:
            if os.path.isfile(CONFIG_FILE):
                os.remove(CONFIG_FILE)
            messagebox.showinfo("Cache Wiped",
                "Launcher cache cleared. The app will now close — "
                "reopen it for a fresh start.", parent=self)
            self.destroy()
        except Exception as e:
            messagebox.showerror("Wipe failed", str(e), parent=self)

    def _browse(self):
        p = filedialog.askopenfilename(
            title="Select A Township Tale.exe",
            filetypes=[("Executable","*.exe"),("All","*.*")])
        if p: self.v_exe.set(p.replace("/","\\")); self._save()

    def _open_server_list(self):
        def on_select(ip, name, port="1757"):
            self.v_ip.set(ip); self.v_port.set(str(port))
            self._selected_kind = "official"; self._save()
        ServerListPanel(self, on_select)

    def _open_community(self):
        def on_select(ip, name, kind, port="1757"):
            self.v_ip.set(ip); self.v_port.set(str(port))
            self._selected_kind = kind; self._save()
        CommunityBrowser(self, on_select)

    def _open_tickets(self):
        TicketsWindow(self, default_host=self.v_ip.get().strip(),
                     default_username=self.v_username.get().strip())

    def _poll_ticket_replies_loop(self):
        """Runs in a background thread for the life of the app -- checks
        every server the user has ever opened a ticket against for a new
        owner reply, purely to drive the flashing Tickets button. Never
        touches the UI directly except through the plain bool flag below,
        which start_flashing_button's own .after() loop reads safely from
        the main thread."""
        while True:
            try:
                username = self.v_username.get().strip()
                found = False
                if username:
                    for host in _known_ticket_hosts():
                        try:
                            token, _ = _get_or_create_token(host, username)
                            resp = ticket_request(host, "list_mine", username, token, timeout=6)
                            if resp.get("status") == "ok" and _has_unseen_owner_reply(host, resp.get("tickets", [])):
                                found = True
                                break
                        except Exception:
                            continue
                self._has_unseen_ticket_reply = found
            except Exception:
                pass
            time.sleep(30)

    def _open_remote_console(self):
        win = RemoteConsoleWindow(self)
        host = self.v_ip.get().strip()
        if host:
            win.v_host.set(host)

    def _open_tavernkeeper(self):
        win = TavernKeeperWindow(self)
        host = self.v_ip.get().strip()
        if host:
            win.v_host.set(host)

    def _open_setup(self):
        exe = self.v_exe.get().strip()
        if not exe or not os.path.isfile(exe):
            messagebox.showerror("Game not found",
                "Please set the path to 'A Township Tale.exe' above first.", parent=self)
            return
        SetupWindow(self, exe, on_status_change=self._refresh_setup_alert)

    def _open_mod_manager(self):
        exe = self.v_exe.get().strip()
        if not exe or not os.path.isfile(exe):
            messagebox.showerror("Game not found",
                "Please set the path to 'A Township Tale.exe' above first.", parent=self)
            return
        if self._mod_manager_win and self._mod_manager_win.winfo_exists():
            self._mod_manager_win.lift(); return
        self._mod_manager_win = ModManagerWindow(
            self, os.path.dirname(exe), side="client",
            on_change=self._refresh_setup_alert)

    def _open_addons(self):
        from client.core import addon_loader
        from tavern_shared.addon_manager_window import AddonManagerWindow
        AddonManagerWindow(self, addon_loader, "Client", get_game_dir=self._current_game_dir)

    def _open_game_settings(self):
        GameSettingsWindow(self)

    def _current_game_dir(self):
        exe = self.v_exe.get().strip()
        return os.path.dirname(exe) if exe and os.path.isfile(exe) else None

    def _on_ip_changed(self, *_):
        """Clear the stale check-status line whenever the IP field changes —
        Check Server and Join Server are both independent from here on, so
        there's no button mode to reset, just the leftover status text.
        Also resets to "official" — a manually-typed IP isn't something we
        know the kind of, so fall back to the flow that's always applied."""
        self._server_ok    = False
        self._checked_host = None
        self._selected_kind = "official"
        self._check_status.set("")
        try: self._check_label.config(fg=MUTED)
        except: pass
        try: self._show_sync_button(False)
        except AttributeError: pass   # fired by the trace before _build_ui ran

    # ── Log helpers ─────────────────────────────────────────────────────────

    def _print(self, msg, tag=""):
        self.log.config(state="normal")
        self.log.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg}\n", tag)
        self.log.see("end"); self.log.config(state="disabled")
        self.update_idletasks()

    def _start_log_tailer(self):
        TAG = {"error":"err","Error":"err","warn":"warn","Warn":"warn",
               "info":"info","Info":"info","debug":"debug","Debug":"debug"}
        def on_line(ts, lv, lg, msg):
            tag = TAG.get(lv,"info")
            short = lg.split(".")[-1] if lg else ""
            pre   = f"[{ts}]" + (f" [{short}]" if short else "")
            self.after(0, lambda: self._append_log(f"{pre} {msg}", tag))
        self._tailer = GameLogTailer(GAME_LOG_PATH, on_line)
        self._tailer.start()

    def _append_log(self, line, tag):
        self.log.config(state="normal")
        self.log.insert("end", line+"\n", tag)
        if float(self.log.index("end-1c").split(".")[0]) > 5000:
            self.log.delete("1.0","1000.0")
        self.log.see("end"); self.log.config(state="disabled")

    # ── Check Server (optional, informational) / Join Server ──────────────────

    def _popen_console_kwargs(self):
        """Hiding MelonLoader's console means redirecting the child's std
        handles at process-creation time — that's what makes it skip
        AllocConsole(). Showing it just means not touching them at all."""
        if self.v_show_melonloader.get():
            return {}
        return {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}

    def _on_join_clicked(self):
        if self._selected_kind == "headless":
            self._do_launch_headless()
        else:
            self._do_launch(password=None)

    def _do_check(self):
        if self._selected_kind == "headless":
            # No port-1762 auth gate exists on these — there's nothing to
            # check. Say so plainly instead of attempting a doomed connection
            # that would just read as a generic failure.
            self._check_status.set(
                "Direct-connect (headless) server — no health check available. "
                "Just press Join Server.")
            try: self._check_label.config(fg=MUTED)
            except: pass
            return
        ip   = self.v_ip.get().strip()
        host = ip if ip else "127.0.0.1"
        self._check_btn.config(state="disabled")
        self._check_status.set(f"Checking {host}…")
        try: self._check_label.config(fg=MUTED)
        except: pass
        threading.Thread(target=self._run_check, args=(host,), daemon=True).start()

    def _run_check(self, host):
        try:
            resp, ms = ping_server(host)
            if resp.get("status") == "pong":
                sv_name = resp.get("server_name", host)
                pw_req  = resp.get("password_required", False)
                wl      = resp.get("whitelist_enabled", False)
                game_port = resp.get("game_port")
                mods_count = resp.get("mods_count")
                lines   = [f"✔  {sv_name}  —  {ms} ms"]
                flags   = []
                if pw_req: flags.append("🔒 Password required")
                if wl:     flags.append("📋 Whitelist active")
                if game_port: flags.append(f"Port {game_port}")
                if mods_count is not None:
                    flags.append(f"🧩 {mods_count} mod{'s' if mods_count != 1 else ''}")
                if flags:  lines.append("  ".join(flags))
                msg = "\n".join(lines)
                self.after(0, lambda: self._check_ok(host, msg, game_port, mods_count))
            else:
                self.after(0, lambda: self._check_fail(f"Unexpected response from {host}"))
        except Exception as e:
            self.after(0, lambda e=e: self._check_fail(f"✘  Cannot reach server — {e}"))

    def _check_ok(self, host, msg, game_port=None, mods_count=None):
        self._server_ok    = True
        self._checked_host = host
        self._check_status.set(msg)
        self._check_label.config(fg=GREEN)
        self._check_btn.config(state="normal")
        # Offered whenever the server answered with a mod count at all, zero
        # included: a server running no mods still means anything currently
        # active here gets deactivated on join, which is a change worth being
        # able to see and apply up front.
        self._show_sync_button(mods_count is not None)
        # The server just told us its actual configured port — trust that
        # over whatever was already in the field, since it's the ground truth.
        if game_port:
            self.v_port.set(str(game_port))

    def _check_fail(self, msg):
        self._server_ok    = False
        self._checked_host = None
        self._check_status.set(msg)
        self._check_label.config(fg=RED)
        self._check_btn.config(state="normal")
        self._show_sync_button(False)

    # ── Mod reconciliation ────────────────────────────────────────────────────

    def _show_sync_button(self, show):
        """Sync Mods only makes sense against a server we just successfully
        checked, so it appears with a good result and goes away the moment that
        result is cleared or replaced."""
        if show:
            self._sync_btn.pack(side="right", padx=(8,0))
        else:
            self._sync_btn.pack_forget()

    def _get_server_mods(self, host):
        """The server's full installed-mods list, for plan_join. Uses the
        ping/pong's mods_hash as a cache key (self._mods_list_cache) so
        rejoining a server whose mods haven't changed since the last check
        skips the extra "mods_list" round trip entirely. Returns [] if the
        server doesn't send mods_hash at all (an older TavernLib/launcher) -
        reconciliation is then a no-op, same as a server with no mods."""
        try:
            resp, _ms = ping_server(host)
        except Exception:
            return []
        mods_hash = resp.get("mods_hash")
        if not mods_hash:
            return []
        cached = self._mods_list_cache.get(host)
        if cached and cached[0] == mods_hash:
            return cached[1]
        try:
            server_mods = fetch_server_mods(host)
        except Exception as e:
            self._print(f"Could not fetch the server's mod list: {e}", "warn")
            return []
        self._mods_list_cache[host] = (mods_hash, server_mods)
        return server_mods

    def _on_sync_mods(self):
        """Brings Mods/ in line with the server just checked, without launching
        the game, so the downloading can be done up front and the actual join is
        instant. Same plan/confirm/apply flow the join path runs; the only
        difference is that nothing launches at the end.

        Unlike that path, everything here runs on a worker thread: the player
        pressed a button and is watching the window, so resolving an index and
        pulling down mods must not freeze it."""
        host = self._checked_host
        if not host:
            return
        exe = self.v_exe.get().strip()
        if not exe or not os.path.isfile(exe):
            messagebox.showerror("Game not found",
                "Please set the path to 'A Township Tale.exe' above first.", parent=self)
            return

        game_dir = os.path.dirname(exe)
        self._set_sync_busy(True)
        self._print(f"Checking which mods {host} needs…", "dim")

        def worker():
            try:
                kind, payload = self._build_mod_plan(game_dir, host)
            except Exception as e:
                self.after(0, lambda e=e: self._sync_failed(e))
                return
            self.after(0, lambda: self._sync_planned(game_dir, host, kind, payload))
        threading.Thread(target=worker, daemon=True).start()

    def _sync_planned(self, game_dir, host, kind, payload):
        """Back on the UI thread with a resolved plan: show the comparison, which
        then does the applying and reports the result in place.

        The window opens whatever the plan says. A blocking one disables Apply
        for itself, and seeing which mod is unavailable beside everything that's
        fine is what tells the player whether to add a source or ask the server
        owner; an all-matching one is the answer to the question they asked by
        pressing the button."""
        self._set_sync_busy(False)
        if kind == "skip":
            self._print(payload, "warn")
            return
        if kind == "error":
            self._print(f"Couldn't work out this server's mods: {payload}", "err")
            return

        plan, server_mods = payload
        self._open_mod_diff(plan, server_mods, game_dir, host, joining=False,
                            on_applied=self._sync_done,
                            on_cancel=lambda win: self._set_sync_busy(False))

    def _sync_done(self, win):
        self._set_sync_busy(False)
        self._print("Mods are now in sync with the server.", "ok")
        win.apply_finished(True, "Your mods now match this server, so joining "
                                 "won't need to download anything.")

    def _sync_failed(self, err):
        self._set_sync_busy(False)
        self._print(f"Mod sync failed: {err}", "err")

    def _set_sync_busy(self, busy):
        """Sync, Check and Join all move the same Mods/ folder or depend on its
        state, so none of them may run while a sync is mid-flight."""
        state = "disabled" if busy else "normal"
        for btn in (self._sync_btn, self._check_btn, self._action_btn):
            btn.config(state=state)

    def _open_mod_diff(self, plan, server_mods, game_dir, host, joining,
                       on_applied=None, on_cancel=None):
        """Shows the full client-vs-server comparison, so nothing is installed,
        moved, or switched off without being seen first, and then hosts the
        applying too: pressing Apply turns the same table into a progress view
        and the window reports the outcome itself.

        Returns straight away. on_applied(window) runs once the render has
        finished, on the UI thread, and owns what happens next (verify, launch,
        or just say it's done). on_cancel(window) runs instead if the window
        closes without a successful apply. With neither, it's a read-only view
        of a plan that can't be applied.

        The window edits the plan before applying: a mod the server doesn't run
        at all is the player's to keep, and keeping it drops that id from
        plan.to_deactivate. So this plan object is the one to render."""
        rows = mods.build_mod_diff(game_dir, server_mods, plan)
        label = f"{host}  ({'joining' if joining else 'sync only'})"
        action = None
        if on_applied is not None:
            action = lambda win: self._apply_mod_plan(win, game_dir, plan,
                                                      on_applied, host)
        return ModDiffWindow(
            self, rows, plan, server_label=label, apply_action=action,
            close_on_success=joining, on_cancel=on_cancel)

    def _apply_mod_plan(self, win, game_dir, plan, on_applied, host=None):
        """Renders the plan on a worker, feeding the window's progress view as it
        goes, then hands back to on_applied. Same work either path does; only
        what follows it differs."""
        self._set_sync_busy(True)
        if host:
            # Remember which recommended mods were turned off for this server
            # before any rendering, so the choice survives even if applying
            # fails partway. Written whole, so taking a choice back sticks too.
            try:
                mods.set_declined(load_cfg(), host, win.declined)
            except Exception as e:
                self._print(f"Could not save your recommended-mod choices: {e}", "warn")

        def report(msg):
            self._print(msg, "dim")
            win.set_status(msg)

        def failed(err):
            self._set_sync_busy(False)
            self._print(f"Applying the mod changes failed: {err}", "err")
            win.apply_finished(False, f"Couldn't apply the changes: {err}")

        def worker():
            try:
                mods.render_active_set(
                    game_dir, plan,
                    lambda m: self.after(0, lambda m=m: report(m)),
                    lambda i, s: self.after(0, lambda i=i, s=s: win.set_step(i, s)))
            except Exception as e:
                self.after(0, lambda e=e: failed(e))
                return
            self.after(0, lambda: on_applied(win))
        threading.Thread(target=worker, daemon=True).start()

    def _build_mod_plan(self, game_dir, host):
        """Shared front half of both mod-sync paths (the Sync Mods button and
        the pre-launch reconcile): cache what's installed, ask the server what
        it runs, and resolve that into a JoinPlan. Changes nothing on disk and
        prompts for nothing, so each caller decides for itself what a given
        outcome means. Safe to call off the UI thread.

        Returns (kind, payload):
          ("skip",  reason)               nothing to sync against, which is a
                                          normal outcome: MelonLoader/TavernLib
                                          aren't installed, the server reported
                                          no mods, or the index is unreachable
          ("error", message)              the server's mods couldn't be resolved
          ("plan",  (plan, server_mods))  a usable plan, possibly blocking
        """
        if not (_melonloader_installed(game_dir) and _tavernlib_installed(game_dir)):
            return "skip", ("Community mods need MelonLoader and TavernLib "
                            "installed before they can load. Install them from "
                            "the Setup window first.")

        # Cache whatever is in Mods/ right now, BEFORE anything below can
        # overwrite it. Without this, a mod installed straight to Mods/ (the
        # Community Mods window, or an older launcher with no cache at all) is
        # unknown to the cache, so rendering an active set that needs a
        # different version of it replaces the folder and destroys the only copy
        # of the version that was there - making a later switch back a
        # re-download instead of the file move the cache exists to guarantee.
        # Idempotent: an already-cached version is skipped.
        try:
            adopted = mods.adopt_installed_mods(game_dir)
            if adopted:
                self._print(f"Cached {len(adopted)} already-installed mod(s): "
                            f"{', '.join(adopted)}", "dim")
        except Exception as e:
            self._print(f"Could not cache the currently-installed mods: {e}", "warn")

        server_mods = self._get_server_mods(host)
        if not server_mods:
            return "skip", ("This server didn't report any mods, so there's "
                            "nothing to sync.")

        cfg = load_cfg()
        repo_bases = mods.list_repos(cfg)
        try:
            index = mods.fetch_indexes(repo_bases)
        except Exception as e:
            self._print(f"Could not check the community mod index: {e}", "warn")
            return "skip", f"Couldn't reach the mod index: {e}"

        pinned = mods.list_pinned(cfg)
        # Recommended mods this player has already turned down for this server
        # stay turned down, rather than being re-offered on every join.
        declined = mods.list_declined(cfg, host)
        try:
            plan = mods.plan_join(game_dir, server_mods, index, repo_bases,
                                  pinned, declined)
        except mods.ModManagerError as e:
            return "error", str(e)
        return "plan", (plan, server_mods)

    def _verify_mod_parity(self, game_dir, server_mods):
        """Every mod the server requires of clients, checked against what's
        actually enabled here at EXACTLY the right version (the same rule the
        server applies on join). Returns the mismatches, empty when parity
        holds. Just a Mods/ scan, so it's cheap enough to run inline.

        Mods the server doesn't require are left out, matching
        ModParity.ValidateClient: the server won't refuse a join over one, so
        neither should this. Checking them here would block launches the server
        would have allowed, which is the whole point of the distinction."""
        required = {m["id"]: m["version"] for m in server_mods
                    if m.get("client_side")
                    and mods.parity_required(m)}
        _, _, installed_mods = mods.handshake_snapshot(game_dir)
        have = {m["id"]: m["version"] for m in installed_mods}
        return [(mid, ver, have.get(mid)) for mid, ver in required.items()
                if have.get(mid) != ver]

    def _reconcile_mods_async(self, host, exe, on_ready):
        """Brings Mods/ in line with this server and then calls on_ready() to
        continue the launch, or never calls it if the launch shouldn't proceed
        (having already said why and re-enabled the buttons).

        Continuation-passing rather than a blocking call, because the slow parts
        here are a repo index fetch and potentially hundreds of megabytes of
        downloads. Run inline they would freeze the window for the whole of it,
        with no redraw and no way to cancel; the download helper's own wall-clock
        cap is 30 minutes, so that freeze is not theoretical. The two slow steps
        run on workers and everything that touches Tk hops back via after().

        The order is fixed: resolve, show the comparison, apply, then verify.
        Verification is deliberately not skipped even when the render reported
        success, since an accepted pin conflict or a fail-soft render can still
        leave a mismatch that would otherwise only surface as an in-game
        rejection."""
        game_dir = os.path.dirname(exe)
        self._set_sync_busy(True)

        def abort(msg=None, tag="err"):
            if msg:
                self._print(msg, tag)
            self._set_sync_busy(False)

        def verified(server_mods, win=None):
            """Parity check after the render. A mismatch here is reported in the
            comparison window when there is one, so the player is looking at the
            table the rows came from rather than a detached error box."""
            mismatched = self._verify_mod_parity(game_dir, server_mods)
            if mismatched:
                lines = "; ".join(f"{mid} needs {ver}, have {have_ver or 'nothing'}"
                                  for mid, ver, have_ver in mismatched)
                self._print(f"Mods still don't match this server ({lines}); "
                            "joining would be rejected, not launching.", "err")
                abort()
                if win is not None:
                    win.apply_finished(False,
                        "Your mods still don't match this server: " + lines +
                        ". Joining would be rejected, so the game wasn't launched.")
                return
            if win is not None:
                win.apply_finished(True)      # closes it, the launch follows
            self._set_sync_busy(False)
            on_ready()

        def planned(kind, payload):
            if kind == "skip":
                self._set_sync_busy(False)
                on_ready()                    # fail open, never block a join
                return
            if kind == "error":
                abort(f"Could not resolve this server's required mods: {payload}")
                return

            plan, server_mods = payload
            if plan.blocking:
                # Show the comparison rather than a bare error: seeing which mod
                # is unavailable, beside everything that is fine, is what tells
                # the player whether to add a source or ask the server owner.
                # No apply_action, so this is a read-only view and can't continue.
                self._print("This server needs mods that aren't in any source "
                            "you've added; not launching.", "err")
                abort()
                self._open_mod_diff(plan, server_mods, game_dir, host, joining=True)
                return

            changed = [e for e in plan.entries if not e.active]
            if not (changed or plan.to_deactivate or plan.needs_repo or plan.pin_conflicts):
                verified(server_mods)         # already correct, nothing to apply
                return

            def cancelled(win):
                # A close after a failed apply has already been explained, both
                # in the log and in the window itself; only a close with no
                # attempt behind it is a decline.
                if win.apply_attempted:
                    self._set_sync_busy(False)
                    return
                abort("Mod changes declined; not launching.", "warn")

            # Hand over to the window: it applies, shows progress, and calls back
            # here when the render is done. Buttons come back on if it's closed
            # without applying, which is also what stops the launch.
            self._set_sync_busy(False)
            self._open_mod_diff(
                plan, server_mods, game_dir, host, joining=True,
                on_applied=lambda win: verified(server_mods, win),
                on_cancel=cancelled)

        def worker():
            try:
                kind, payload = self._build_mod_plan(game_dir, host)
            except Exception as e:
                self.after(0, lambda e=e: abort(f"Could not check this server's mods: {e}"))
                return
            self.after(0, lambda: planned(kind, payload))
        threading.Thread(target=worker, daemon=True).start()

    # ── Launch ────────────────────────────────────────────────────────────────

    def _try_headless_fallback(self, display_host, resolved_host):
        """Only reached when the normal auth service at port 1762 couldn't
        be contacted at all. Checks the community list for a *currently
        registered* headless server at this address, and only proceeds with
        an unauthenticated join if that's confirmed — this is a fallback
        for a known, already-vouched-for server, never a way to silently
        skip auth for an address that just happens to be unreachable or
        misconfigured. Tries the exact string the player typed first (in
        case it matches a verified hostname), then the resolved IP (in case
        they typed a hostname that isn't what the server registered with,
        but happens to point at the same place)."""
        candidates = [display_host]
        if resolved_host and resolved_host != display_host:
            candidates.append(resolved_host)

        for address in candidates:
            try:
                params = urlencode({"address": address})
                req = urllib.request.Request(f"{COMMUNITY_API}/lookup?{params}",
                    headers={"User-Agent": "TavernLauncher/1.0"})
                with urllib.request.urlopen(req, timeout=6) as resp:
                    data = json.loads(resp.read().decode())
            except Exception as e:
                self._print(f"Could not check community list: {e}", "warn")
                continue

            if data.get("found") and data.get("kind") == "headless":
                self._print(f"'{address}' is a known headless server — "
                            "joining directly, no auth.", "warn")
                if data.get("port"):
                    self.v_port.set(str(data["port"]))
                self._selected_kind = "headless"
                self._do_launch_headless()
                return True

        return False

    def _do_launch(self, password, _token_state=None):
        exe      = self.v_exe.get().strip()
        username = self.v_username.get().strip()
        platform = self.v_platform.get()
        platform = PLATFORM_DISPLAY_TO_BACKEND.get(platform, platform)
        ip       = self.v_ip.get().strip()
        display_host = ip if ip else "127.0.0.1"
        # Resolved once and used everywhere identity-sensitive matters (token
        # lookup, the auth handshake, and the game's own launch arg) — a
        # server reachable by both a hostname and its IP is still one server,
        # and needs to be treated as one for token purposes. Without this,
        # joining once via "myserver.com" and later via its raw IP would look
        # like two different servers locally, generate two different tokens,
        # and get rejected as "that name is taken by someone else" the second
        # time — even though it's the same account on the same server.
        host = _resolve_ip_for_game(display_host)

        if not exe or not os.path.isfile(exe):
            messagebox.showerror("Not found",
                "Could not find the game.\nPlease browse to 'A Township Tale.exe'.", parent=self)
            return
        if not username:
            messagebox.showerror("Missing name",
                "Please enter your username before connecting.", parent=self)
            return
        if len(username) > USERNAME_MAX_LEN:
            messagebox.showerror("Name too long",
                f"Usernames can be at most {USERNAME_MAX_LEN} characters.", parent=self)
            return
        if not _is_valid_username(username):
            messagebox.showerror("Invalid name",
                "Usernames can only contain letters, numbers, spaces, hyphens, and underscores.", parent=self)
            return

        self._save()
        self._action_btn.config(state="disabled")
        self._print(f"Authenticating '{username}'…", "dim")

        # Resolve (and, on first contact with this server, create) the token
        # once per launch attempt so a password retry doesn't regenerate it.
        if _token_state is None:
            had_token_before = _any_token_files_exist()
            token, token_is_new = _get_or_create_token(host, username)
        else:
            token, token_is_new, had_token_before = _token_state

        user_id, error, quest_scene_required = authenticate(host, username, token, password=password)

        if error == "NEEDS_PASSWORD":
            self._action_btn.config(state="normal")
            pw = simpledialog.askstring("Password Required",
                "This server requires a password:", show="*", parent=self)
            if pw: self._do_launch(password=pw,
                                    _token_state=(token, token_is_new, had_token_before))
            return

        if error == "NOT_WHITELISTED":
            self._action_btn.config(state="normal")
            if messagebox.askyesno("Whitelist Required",
                    "This server requires a whitelist, and you're not on it yet.\n\n"
                    "Would you like to apply to this server?", parent=self):
                was_new, apply_error = register_whitelist_application(host, username)
                if apply_error:
                    messagebox.showerror("Couldn't send application", apply_error, parent=self)
                elif was_new:
                    messagebox.showinfo("Application Sent",
                        "Your application was sent to the server, check back later.", parent=self)
                else:
                    messagebox.showinfo("Already Applied",
                        "You already have a pending application for this server.", parent=self)
            return

        if error and error.startswith("CANNOT_REACH::"):
            detail = error.split("::", 1)[1]
            self._print(f"No official auth service at {host}:{AUTH_PORT} — "
                        "checking community list for a headless registration…", "warn")
            if self._try_headless_fallback(display_host, host):
                return
            self._print(f"Rejected: Cannot reach server at {host}:{AUTH_PORT} — {detail}", "err")
            self._action_btn.config(state="normal")
            return

        if error:
            self._print(f"Rejected: {error}", "err")
            self._action_btn.config(state="normal")
            return

        self._print(f"Welcomed as {username} (ID {user_id})", "ok")
        # Skipped for localhost specifically: this resync clears and
        # repopulates the flat CustomAssets/ folder that TavernLib reads
        # from, regardless of whether the game process it's running inside
        # is acting as your server or as a client -- there's no per-role
        # distinction at that level. If you're hosting your own server AND
        # joining it from the same machine, this resync was touching the
        # exact same folder your own server process depends on, and could
        # race with it (briefly empty/incomplete right as the server
        # itself starts up and does its own one-time scan). A local server
        # owner's folder is already exactly what's needed -- there's
        # nothing to download from yourself over the network.
        if host not in ("127.0.0.1", "localhost", socket.gethostname()):
            run_post_auth_hooks(host, self._print)
        self._show_token_button()
        if token_is_new and not had_token_before:
            # The very first token file this launcher has ever created on this
            # machine — open the explainer immediately instead of waiting for a click.
            self._on_token_button_click()

        # Record in recent — use server_name from last ping if available
        cfg = load_cfg()
        sv_name = display_host
        status_text = self._check_status.get()
        if status_text.startswith("✔"):
            # Parse the server name out of the status line "✔  ServerName  —  Xms"
            try: sv_name = status_text.split("✔")[1].split("—")[0].strip()
            except: pass
        recent = [r for r in cfg.get("recent_servers",[]) if r.get("ip") != display_host]
        recent.insert(0, {"name": sv_name, "ip": display_host, "port": self.v_port.get()})
        cfg["recent_servers"] = recent[:20]
        save_cfg(cfg)

        # Mod reconciliation can download hundreds of megabytes, so it runs off
        # the UI thread and calls back only when the launch should go ahead.
        # Everything above this point is quick.
        self._reconcile_mods_async(host, exe, lambda: self._launch_game(
            exe, ip, host, platform, username, user_id, token,
            lambda: self._do_launch(password=None),
            quest_scene_required=quest_scene_required))

    def _launch_game(self, exe, ip, host, platform, username, user_id, token,
                     relaunch, quest_scene_required=False):
        """Builds the tokens and starts the game. The shared tail of both join
        paths, which differ only in the token they carry (the auth flow has a
        real one, the direct flow has none) and where a rejection should rejoin
        to. Runs on the UI thread and returns immediately: everything slow has
        already finished by the time anything reaches here."""
        # Report what's actually enabled in Mods/ (after the render) so the
        # server's exact-version parity check has something to compare against;
        # see build_tokens' mods_claim.
        mods_claim = ""
        try:
            _, _, mods_list = mods.handshake_snapshot(os.path.dirname(exe))
            mods_claim = json.dumps({m["id"]: m["version"] for m in mods_list})
        except Exception as e:
            self._print(f"Could not read installed mods for the join handshake: {e}", "warn")

        access, refresh, identity = build_tokens(user_id, username, token, mods_claim)
        args = [exe, "/force_offline",
                "/access_token", access, "/refresh_token", refresh,
                "/identity_token", identity, "/join_local_server"]

        if platform in ("none", "fly"):
            args.insert(-1, "/fly")
        elif platform:
            args[-1:] = ["/vrmode", platform, "/join_local_server"]
        if ip:
            # Already resolved to a canonical IP by the caller, the same value
            # used for the token lookup and the auth handshake, so all three
            # agree on which server this is.
            args += ["/dev_server_ip", host]
        args += ["/dev_server_port", str(_valid_port(self.v_port.get()))]
        if self.v_debug_helper.get():
            args.append("/debug_helper")
        if quest_scene_required:
            # Not a local preference the player can toggle — this server
            # told us during the auth handshake that it needs this, so the
            # client has to match, not choose for itself.
            args.append("/questScene")

        # Delete any stale rejection file before launching, so a file present
        # after THIS process exits was written by THIS attempt (the actual
        # staleness guard is that we wait for this exact process and nothing
        # else runs in between; see _watch_for_rejection).
        _delete_last_rejection_file()

        self._print(f"Launching on {platform or 'default'}…", "warn")
        try:
            # Whether MelonLoader's own console window shows up is controlled
            # by the Show MelonLoader toggle: hiding it means redirecting the
            # child's std handles at creation time (which is what makes it
            # skip AllocConsole); showing it means leaving them alone.
            proc = subprocess.Popen(args, cwd=os.path.dirname(exe),
                                    **self._popen_console_kwargs())
            self._print(f"Game running (PID {proc.pid})", "ok")
            threading.Thread(target=self._watch_for_rejection,
                             args=(proc, host, relaunch), daemon=True).start()
        except Exception as e:
            self._print(f"Launch failed: {e}", "err")
        self._action_btn.config(state="normal")

    def _watch_for_rejection(self, proc, host, relaunch):
        """Runs on a background thread once the game process is launched.
        Waits for it to exit, then checks for TavernLib's last_rejection.json,
        written by this same client's own TavernLib instance right before
        exiting, if (and only if) the server denied the join for a mod-mismatch
        reason. The launch path already deleted any stale copy before this
        process started, so a file present now was written by this exact
        attempt.

        relaunch is the caller's own way back in, so recovery rejoins via the
        path the player actually used - _do_launch for the auth-gated flow,
        _do_launch_headless for the direct one. Hardcoding _do_launch here
        would send a headless join through an auth handshake its server never
        answers."""
        proc.wait()
        path = _last_rejection_path()
        if not os.path.isfile(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception as e:
            # Bound as a default, not captured: Python clears the `as e` name
            # when the except block ends, and this lambda runs later on the UI
            # thread, so a free reference would be a NameError instead of the
            # warning it is meant to print.
            self.after(0, lambda e=e: self._print(
                f"Could not read the rejection file: {e}", "warn"))
            return
        finally:
            try: os.remove(path)   # consumed either way, never re-act on it twice
            except OSError: pass

        if payload.get("schema") != 1:
            return
        server = payload.get("server") or {}
        # Extra cross-check beyond the delete-before-launch guard above: a
        # host mismatch means this file wasn't actually about the server we
        # just tried to join.
        if server.get("host") and server.get("host") != host:
            return
        missing = payload.get("missing") or []
        if not missing:
            return
        self.after(0, lambda: self._offer_rejection_recovery(payload, relaunch))

    def _offer_rejection_recovery(self, payload, relaunch):
        """The server rejected the join over exact mods the client didn't have.
        Offers to fetch exactly those (and their dependencies), same
        confirm-before-install and never-auto-add-a-repo rules as everywhere
        else, then rejoins.

        Only the prompt runs here, on the UI thread; the index fetch and the
        downloads go to a worker, same as every other install path in this
        file. Doing them inline would freeze the whole launcher for the length
        of a download - up to the 30-minute wall-clock cap - with no window
        redraw and no way to cancel."""
        missing = payload.get("missing") or []
        names = "\n".join(f"  • {m['id']} {m['version']}" for m in missing)
        if not messagebox.askyesno("Mods needed to rejoin",
                f"That server rejected the join because these mods didn't "
                f"match:\n\n{names}\n\nInstall the exact versions it needs "
                f"and try rejoining?", parent=self):
            return

        exe = self.v_exe.get().strip()
        game_dir = os.path.dirname(exe)
        self._action_btn.config(state="disabled")
        self._print("Recovering the missing mods…", "warn")

        def worker():
            try:
                cfg = load_cfg()
                repo_bases = mods.list_repos(cfg)
                index = mods.fetch_indexes(repo_bases)
                roots, dependencies = mods.resolve_missing_mods(missing, index, repo_bases)
                libraries = mods.collect_library_dependencies(roots + dependencies)
                for mod in roots + dependencies:
                    mods.install_mod(
                        game_dir, mod,
                        lambda m, _i=mod: self.after(0, lambda: self._print(
                            f"{_i.id}: {m}", "dim")))
                for lib in libraries:
                    mods.install_library_dependency(
                        game_dir, lib,
                        lambda m, _l=lib: self.after(0, lambda: self._print(
                            f"{_l.filename}: {m}", "dim")))
            except Exception as e:
                self.after(0, lambda e=e: self._recovery_failed(e))
                return
            self.after(0, lambda: self._recovery_done(relaunch))

        threading.Thread(target=worker, daemon=True).start()

    def _recovery_failed(self, err):
        self._action_btn.config(state="normal")
        messagebox.showerror("Recovery failed", str(err), parent=self)

    def _recovery_done(self, relaunch):
        self._action_btn.config(state="normal")
        self._print("Recovered the missing mods, rejoining…", "ok")
        relaunch()

    def _do_launch_headless(self):
        """Same launch as _do_launch, minus the port-1762 auth handshake, for
        servers hosted directly via the game itself with no auth gate.
        user_id has no server to come from here, so it's derived locally
        instead (see _headless_user_id); everything after that point is
        identical to the official flow.

        Mod reconciliation still runs, because it is a separate concern from
        auth. TavernLib's join-time parity check (PlayerJoinFilter) is a
        Harmony patch on the join pipeline, not part of the auth service, so it
        applies to this path exactly as it does to the official one: a server
        reached this way still rejects a client whose mods don't match.
        Skipping the mod work here would therefore mean presenting an empty
        mods claim to a server that requires mods and being rejected every
        single time, with no rejection watcher running to recover from it
        either. If the host serves no mod info at all, _reconcile_mods_async is
        a no-op."""
        exe      = self.v_exe.get().strip()
        username = self.v_username.get().strip()
        platform = self.v_platform.get()
        platform = PLATFORM_DISPLAY_TO_BACKEND.get(platform, platform)
        ip       = self.v_ip.get().strip()
        host     = ip if ip else "127.0.0.1"

        if not exe or not os.path.isfile(exe):
            messagebox.showerror("Not found",
                "Could not find the game.\nPlease browse to 'A Township Tale.exe'.", parent=self)
            return
        if not username:
            messagebox.showerror("Missing name",
                "Please enter your username before connecting.", parent=self)
            return
        if len(username) > USERNAME_MAX_LEN:
            messagebox.showerror("Name too long",
                f"Usernames can be at most {USERNAME_MAX_LEN} characters.", parent=self)
            return
        if not _is_valid_username(username):
            messagebox.showerror("Invalid name",
                "Usernames can only contain letters, numbers, spaces, hyphens, and underscores.", parent=self)
            return

        self._save()
        self._action_btn.config(state="disabled")
        self._print(f"Joining headless server directly (no auth gate) as '{username}'…", "warn")

        user_id = _headless_user_id(username)

        cfg = load_cfg()
        recent = [r for r in cfg.get("recent_servers",[]) if r.get("ip") != host]
        recent.insert(0, {"name": host, "ip": host, "port": self.v_port.get()})
        cfg["recent_servers"] = recent[:20]
        save_cfg(cfg)

        # Same resolution the /dev_server_ip arg uses, so the host the
        # reconcile and the rejection watcher key off is the one the game
        # actually connects to (TavernLib records the resolved address in the
        # rejection payload, and _watch_for_rejection cross-checks against it).
        resolved_host = _resolve_ip_for_game(host)
        # Off the UI thread, same as the auth-gated path. No token to carry
        # here, and a rejection rejoins through this same direct flow.
        self._reconcile_mods_async(resolved_host, exe, lambda: self._launch_game(
            exe, ip, resolved_host, platform, username, user_id, "",
            self._do_launch_headless))

