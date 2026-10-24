"""
The main Server launcher window -- exe path, Mods/Patch buttons, server
settings/player-manager/console/tickets buttons, and the game log panel.
The header's button row is NOT hardcoded beyond Discord + Copy Console
Token: anything else (e.g. custom_models' "Custom Models" button) comes
from server.core.header_registry, if that addon is part of this build.
"""
import os
import sys
import time
import json
import subprocess
import threading
import webbrowser
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

from tavern_shared.theme import (
    BG, SURF, SURF2, BORDER, AMBER, AMBERDIM, PARCH, MUTED, GREEN, RED, CYAN, MONO,
    _btn, _field, _hint, _section_label, _divider, _mk_scrollbar,
)
from tavern_shared.window_chrome import _start_hidden, _finish_dark_window, _set_window_icon, _header_crop_box
from tavern_shared.help_pane import attach_help_tab
from tavern_shared.assets import _HEADER_BANNER_IMG
try:
    from PIL import Image as _PILImage, ImageTk as _PILImageTk, ImageEnhance as _PILImageEnhance
except ImportError:
    pass
from tavern_shared.log_tailer import GameLogTailer
from tavern_shared.game_guard import game_is_running
from tavern_shared import mods
from tavern_shared.launcher_common import LauncherCommon
from tavern_shared.log_archive import archive_log_now, should_auto_archive

from server.core.data_store import (
    load_cfg, save_cfg, CONFIG_FILE, GAME_LOG_PATH, DISCORD_URL,
    CONSOLE_TOKEN_FILE, PLAYERS_SAVE, SERVER_DATA_DIR, load_server_settings, save_server_settings,
    _load_whitelist_requests, _tickets_needing_owner_attention,
)
from server.core.server_backup import backup_due, create_backup, prune_backups
from tavern_shared.flashing import start_flashing_button
from server.core.auth_service import (
    start_auth_service, AUTH_PORT, build_console_token, build_server_tokens,
    invalidate_mods_snapshot,
)
from server.core.console_window import ConsoleWindow
from server.core.tickets_window import TicketsWindow
from server.core.player_manager import PlayerManagerWindow
from server.core.server_settings import ServerSettingsWindow
from server.version import APP_VERSION, UPDATE_APP_FOLDER

try:
    import updater as _updater
except ImportError:
    _updater = None

class ServerLauncher(LauncherCommon, tk.Tk):
    def __init__(self):
        super().__init__()
        _start_hidden(self)
        self.title("TavernLauncher - Server")
        self.configure(bg=BG)
        # Same reasoning as the client launcher — this window's log is the
        # thing worth resizing for, so let the whole window resize.
        self.resizable(True, True)
        self.geometry("560x760")  # placeholder; resized to fit content below
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
        self._proc     = None
        self._auth_on  = False
        self._tailer   = None
        self._mgr_win  = None
        self._sett_win = None
        self._console_win = None
        self._tickets_win = None
        self._served_game_dir = None   # read by the auth service's threads
        self._starting = False         # between Open Server and the launch
        self._init_launcher_common()
        self._build_ui()
        self._load()
        self._start_log_tailer()
        self._start_auto_archive_check()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        # Same reasoning as the client launcher — fit_w used to be a
        # hardcoded guess that went stale every time a row gained another
        # button/checkbox; measuring it the same way fit_h already was is
        # what actually keeps this correct going forward.
        self.update_idletasks()
        fit_w = max(560, self.winfo_reqwidth())
        fit_h = self.winfo_reqheight()
        self.geometry(f"{fit_w}x{fit_h}")
        self.minsize(fit_w, fit_h)
        _finish_dark_window(self)
        attach_help_tab(self)

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
        self._mod_manager_btn = _btn(btn_row_mods, "📦 Mod Manager", self._open_mod_manager,
             font=("Segoe UI",9), pady=5, padx=10)
        self._mod_manager_btn.pack(side="left", padx=(6,0))
        _section_label(self, "GAME PORT")
        pf2 = _field(self)
        self.v_port = tk.StringVar(value="1757")
        tk.Entry(pf2, textvariable=self.v_port, bg=SURF, fg=PARCH,
                 insertbackground=AMBER, relief="flat", font=("Consolas",10), bd=6).pack(fill="x")
        _hint(self, "Forward 1757–1762 (UDP+TCP) for remote players.")
        _divider(self)
        self._sv_name_var = tk.StringVar(value="—")
        nf = tk.Frame(self, bg=SURF, highlightbackground=BORDER, highlightthickness=1)
        nf.pack(fill="x", padx=20, pady=(0,4))
        tk.Label(nf, text="SERVER NAME", bg=SURF, fg=MUTED,
                 font=("Segoe UI",8,"bold")).pack(side="left", padx=10, pady=6)
        tk.Label(nf, textvariable=self._sv_name_var, bg=SURF, fg=AMBER,
                 font=("Georgia",11,"bold")).pack(side="left", padx=4, pady=6)
        tr = tk.Frame(self, bg=BG)
        tr.pack(fill="x", padx=20, pady=(4,4))
        _btn(tr, "⚙ Settings", self._open_settings, font=("Segoe UI",9),
             pady=7, padx=12).pack(side="left")
        self._players_btn = _btn(tr, "👤 Players",  self._open_manager,  font=("Segoe UI",9),
             pady=7, padx=12)
        self._players_btn.pack(side="left", padx=6)
        self._tickets_btn = _btn(tr, "🎫 Tickets",  self._open_tickets,  font=("Segoe UI",9),
             pady=7, padx=12)
        self._tickets_btn.pack(side="left", padx=6)
        start_flashing_button(self._players_btn,
            lambda: len(_load_whitelist_requests()) > 0,
            normal_bg=SURF2, alert_bg=RED)
        start_flashing_button(self._tickets_btn,
            lambda: _tickets_needing_owner_attention() > 0,
            normal_bg=SURF2, alert_bg=RED)
        _btn(tr, "🖥 Console",  self._open_console,  font=("Segoe UI",9),
             pady=7, padx=12).pack(side="left", padx=6)
        _btn(tr, "📁 Saves",    self._open_saves,    font=("Segoe UI",9),
             pady=7, padx=12).pack(side="right")
        _divider(self)
        sf = tk.Frame(self, bg=SURF, highlightbackground=BORDER, highlightthickness=1)
        sf.pack(fill="x", padx=20, pady=(0,6))
        self._dot = tk.Canvas(sf, width=10, height=10, bg=SURF, highlightthickness=0)
        self._dot.pack(side="left", padx=(10,6), pady=8)
        self._dot.create_oval(1,1,9,9, fill=MUTED, outline="", tags="dot")
        self._status_var = tk.StringVar(value="Offline")
        tk.Label(sf, textvariable=self._status_var, bg=SURF, fg=MUTED,
                 font=("Segoe UI",10)).pack(side="left")
        self._pid_var = tk.StringVar()
        tk.Label(sf, textvariable=self._pid_var, bg=SURF, fg=AMBERDIM,
                 font=MONO).pack(side="right", padx=10)
        bf = tk.Frame(self, bg=BG)
        bf.pack(fill="x", padx=20, pady=(0,6))
        self._btn_start = _btn(bf, "⚔   Open Server", self._start, "success",
                               font=("Georgia",12,"bold"), pady=12)
        self._btn_start.pack(fill="x", pady=(0,6))
        self._btn_stop = _btn(bf, "✕   Close Server", self._stop, "danger",
                              font=("Georgia",12,"bold"), pady=12)
        self._btn_stop.pack(fill="x")
        self._btn_stop.config(state="disabled")
        log_header = tk.Frame(self, bg=BG)
        log_header.pack(fill="x")
        tk.Label(log_header, text="SERVER LOG", bg=BG, fg=MUTED,
                 font=("Georgia",8,"bold")).pack(side="left", padx=22, pady=(7,3))
        self._archive_log_btn = _btn(log_header, "🗄 Archive Log", self._on_archive_log_click,
             font=("Segoe UI",7), pady=2, padx=6)
        self._archive_log_btn.pack(side="right", padx=(0,20))
        lf = tk.Frame(self, bg=BG)
        lf.pack(fill="both", expand=True, padx=20, pady=(0,8))
        lb = tk.Frame(lf, bg=SURF, highlightbackground=BORDER, highlightthickness=1)
        lb.pack(fill="both", expand=True)
        self.log = tk.Text(lb, bg=SURF, fg="#b09a78", font=MONO,
                           relief="flat", bd=0, state="disabled", height=9,
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
        self.v_show_game = tk.BooleanVar(value=False)
        tk.Checkbutton(df, text="Show Game", variable=self.v_show_game,
                       command=self._save, bg=BG, fg=MUTED, selectcolor=SURF,
                       activebackground=BG, activeforeground=AMBER,
                       padx=0, pady=0, borderwidth=0, highlightthickness=0,
                       font=("Segoe UI",7)).pack(side="left", padx=(8,0))
        # Unlike the other toggles on this row, this one isn't a purely
        # local launcher preference — a connecting client's own client.exe
        # needs to know whether this server requires it, so the value has
        # to live in server_settings.json (where _handle_auth can read it),
        # not the launcher's own local cfg like the others here.
        ss_for_quest_scene = load_server_settings()
        self.v_quest_scene = tk.BooleanVar(value=ss_for_quest_scene.get("quest_scene", False))
        tk.Checkbutton(df, text="Quest Scene", variable=self.v_quest_scene,
                       command=self._save_quest_scene, bg=BG, fg=MUTED, selectcolor=SURF,
                       activebackground=BG, activeforeground=AMBER,
                       padx=0, pady=0, borderwidth=0, highlightthickness=0,
                       font=("Segoe UI",7)).pack(side="left", padx=(8,0))
        self.v_tutorial_scene = tk.BooleanVar(value=ss_for_quest_scene.get("tutorial_scene", False))
        tk.Checkbutton(df, text="Tutorial Scene", variable=self.v_tutorial_scene,
                       command=self._save_tutorial_scene, bg=BG, fg=MUTED, selectcolor=SURF,
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
        canvas.create_text(18, 32, text="⚒", fill=AMBER, font=("Georgia",22), anchor="w")
        canvas.create_text(66, 21, text="The Modding Tavern", fill=AMBER,
                           font=("Georgia",14,"bold"), anchor="w")
        canvas.create_text(66, 42, text=f"Server Launcher  ·  v{APP_VERSION}", fill=AMBER,
                           font=("Segoe UI",9), anchor="w")

        from server.core.header_registry import ordered_buttons
        self._header_widgets = {}
        self._header_items = {}
        self._header_order = []
        for b in ordered_buttons():
            btn = tk.Button(canvas, text=b["icon_text"], bg=SURF2, fg=PARCH,
                             activebackground=AMBERDIM, activeforeground="#ffd080",
                             relief="flat", bd=0, cursor="hand2",
                             font=("Segoe UI",9), padx=10, pady=4,
                             command=getattr(self, b["command_attr"]))
            item = canvas.create_window(0, 32, anchor="e", window=btn)
            self._header_widgets[b["key"]] = btn
            self._header_items[b["key"]] = item
            self._header_order.append(b["key"])
            if b["key"] == "discord":
                self._discord_btn = btn
                self._discord_btn_item = item
            elif b["key"] == "copy_token":
                self._copy_token_btn = btn
                self._copy_token_btn_item = item

        canvas.bind("<Configure>", self._on_header_resize)
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

    def _open_discord(self):
        webbrowser.open(DISCORD_URL)

    def _copy_console_token(self):
        try:
            with open(CONSOLE_TOKEN_FILE) as f:
                token = f.read().strip()
            self.clipboard_clear()
            self.clipboard_append(token)
            # Brief visual feedback
            self._copy_token_btn.config(text="✓ Copied!")
            self.after(1500, lambda: self._copy_token_btn.config(text="📋 Copy Console Token"))
        except FileNotFoundError:
            messagebox.showinfo("No token yet",
                "Start the server first to generate a console token.", parent=self)

    def _on_header_resize(self, event):
        """Rescales the banner to fill the header exactly, and keeps the
        Discord badge right-aligned — a Canvas doesn't auto-stretch or
        reposition its own children, so this has to be done by hand."""
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
        x = w - 14
        if not self._header_order:
            return
        first_key = self._header_order[0]
        self._header_canvas.coords(self._header_items[first_key], x, hgt // 2)
        prev_width = self._header_widgets[first_key].winfo_reqwidth()
        for key in self._header_order[1:]:
            x -= prev_width + 8
            self._header_canvas.coords(self._header_items[key], x, hgt // 2)
            prev_width = self._header_widgets[key].winfo_reqwidth()

    def _load(self):
        cfg = load_cfg()
        self.v_exe.set(cfg.get("server_exe",""))
        self.v_port.set(cfg.get("server_port","1757"))
        self.v_debug_helper.set(cfg.get("debug_helper", False))
        self.v_show_melonloader.set(cfg.get("show_melonloader", False))
        self.v_show_game.set(cfg.get("show_game", False))
        ss = load_server_settings()
        self._sv_name_var.set(ss.get("name","—"))
        self._print("Tavern server ready.", "ok")
        self._print("Set game exe and click Open Server.", "dim")
        # Immediate check at startup — the trace-driven debounce from
        # v_exe.set above will also fire, but 800ms later; this makes the
        # Patch/Mods button states correct from the very first frame.
        self._refresh_tool_states()
        # Update check runs a couple seconds after startup, off the UI
        # thread, so it never delays the window actually appearing.
        self.after(2000, self._check_for_launcher_update)
        self.after(1200, lambda: self._report_partial_update(_updater))

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
                # on success; anything it raises comes before the installed
                # copy is touched.
            except Exception as e:
                self.after(0, lambda e=e: messagebox.showerror("Update failed",
                    f"Couldn't apply the update:\n{e}\n\n"
                    "The current version is unaffected — nothing was replaced.", parent=self))
        threading.Thread(target=worker, daemon=True).start()

    def _save(self):
        save_cfg({**load_cfg(), "server_exe": self.v_exe.get(),
                  "server_port": self.v_port.get(),
                  "debug_helper": self.v_debug_helper.get(),
                  "show_melonloader": self.v_show_melonloader.get(),
                  "show_game": self.v_show_game.get()})

    def _save_quest_scene(self):
        """Separate from _save() above on purpose — this setting isn't a
        local launcher preference like the others on that same row, it's
        something a connecting client needs to learn about via the auth
        handshake, so it has to live in server_settings.json rather than
        this launcher's own local cfg."""
        if self.v_quest_scene.get():
            self.v_tutorial_scene.set(False)
        self._save_scenes()

    def _save_tutorial_scene(self):
        if self.v_tutorial_scene.get():
            self.v_quest_scene.set(False)
        self._save_scenes()

    def _save_scenes(self):
        ss = load_server_settings()
        ss["quest_scene"] = self.v_quest_scene.get()
        ss["tutorial_scene"] = self.v_tutorial_scene.get()
        save_server_settings(ss)

    def _wipe_cache(self):
        if not messagebox.askyesno("Wipe Launcher Cache",
                "This will delete this launcher's saved settings file:\n\n"
                f"{CONFIG_FILE}\n\n"
                "That includes your saved game path, port, and toggle "
                "preferences — giving you a completely fresh, unconfigured "
                "launcher next time it starts.\n\n"
                "Your player data, server settings, tokens, patch, and "
                "installed mods are NOT affected — only this launcher's own "
                "remembered fields.\n\n"
                "This cannot be undone. Continue?", icon="warning", parent=self):
            return
        try:
            if os.path.isfile(CONFIG_FILE):
                os.remove(CONFIG_FILE)
            messagebox.showinfo("Cache Wiped",
                "Launcher cache cleared. The app will now close — "
                "reopen it for a fresh start.", parent=self)
            self._on_close()
        except Exception as e:
            messagebox.showerror("Wipe failed", str(e), parent=self)

    def _browse(self):
        p = filedialog.askopenfilename(
            title="Select A Township Tale.exe",
            filetypes=[("Executable","*.exe"),("All","*.*")])
        if p: self.v_exe.set(p.replace("/","\\")); self._save()

    def _open_settings(self):
        if self._sett_win and self._sett_win.winfo_exists():
            self._sett_win.lift(); return
        def on_save(name):
            self._sv_name_var.set(name)
        self._sett_win = ServerSettingsWindow(self, on_save)

    def _open_manager(self):
        if self._mgr_win and self._mgr_win.winfo_exists():
            self._mgr_win.lift(); return
        self._mgr_win = PlayerManagerWindow(self)

    def _open_tickets(self):
        if self._tickets_win and self._tickets_win.winfo_exists():
            self._tickets_win.lift(); return
        self._tickets_win = TicketsWindow(self)

    def _open_console(self):
        if self._console_win and self._console_win.winfo_exists():
            self._console_win.lift(); return
        self._console_win = ConsoleWindow(self)

    MOD_SIDE = "server"

    def _mod_manager_kwargs(self):
        return {"server_scene": self._server_scene}

    def _server_scene(self):
        """The scene this server is set to run, by the names a mod manifest's
        `scenes` uses. Tutorial wins over quest, as it does for the launch
        arguments below and in TavernLib."""
        if self.v_tutorial_scene.get():
            return "tutorial"
        return "quest" if self.v_quest_scene.get() else "normal"

    def _warn_mods_outside_scene(self, game_dir):
        """Names every enabled mod that says it doesn't work in the scene this
        start is about to run. A warning, not a block: which mods to run is the
        operator's call, and they may know better than a manifest. A headless
        server, with nobody to tell, skips such a mod instead."""
        scene = self._server_scene()
        try:
            installed = mods.list_installed_mods(game_dir)
        except Exception:
            return
        for rec in installed:
            if not rec.get("enabled"):
                continue
            try:
                scenes = mods.mod_scenes(rec)
            except mods.ModManagerError:
                continue
            if not mods.runs_in_scene(scenes, scene):
                self._print(f"{rec.get('name', rec['id'])} says it works in the "
                            f"{', '.join(scenes)} scene{'s' if len(scenes) > 1 else ''}, "
                            f"not the {scene} scene this server is starting in.", "warn")

    def _mods_written(self):
        """The handshake (this launcher's auth service and TavernLib's join
        check) reads Mods/ live, but the running session loaded its mods at
        startup, so joining clients get matched against a set it isn't
        running. Nothing here can restart the game safely, so it says so."""
        invalidate_mods_snapshot()
        if game_is_running(self._proc):
            self._print("Mods changed while the server is running: the changes "
                        "take effect after a server restart. Until then, joining "
                        "players are matched against the NEW mod set while the "
                        "running session still has the old one - restart soon.",
                        "warn")

    def _open_addons(self):
        from server.core import addon_loader
        from tavern_shared.addon_manager_window import AddonManagerWindow
        AddonManagerWindow(self, addon_loader, "Server", get_game_dir=self._current_game_dir)

    def _current_game_dir(self):
        exe = self.v_exe.get().strip()
        return os.path.dirname(exe) if exe and os.path.isfile(exe) else None

    def _open_saves(self):
        try: os.makedirs(PLAYERS_SAVE, exist_ok=True); os.startfile(PLAYERS_SAVE)
        except Exception as e: messagebox.showerror("Error", str(e), parent=self)

    def _print(self, msg, tag=""):
        def _do():
            self.log.config(state="normal")
            self.log.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg}\n", tag)
            self.log.see("end"); self.log.config(state="disabled")
        self.after(0, _do)

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

    def _archive_log_with_tailer_paused(self, on_done):
        # The log tailer keeps its own read handle open, which blocks
        # renaming the file -- pause it, archive, then restart it.
        def worker():
            self._tailer.stop_and_wait(timeout=2)
            ok, result, locked = archive_log_now(GAME_LOG_PATH)
            self.after(0, self._start_log_tailer)
            self.after(0, lambda: on_done(ok, result, locked))
        threading.Thread(target=worker, daemon=True).start()

    def _on_archive_log_click(self):
        def after_pausing_tailer(ok, result, locked):
            if ok:
                self._print(f"Log archived to {result}", "ok")
                return
            if not locked:
                self._print(f"Couldn't archive log: {result}", "err")
                return

            # Still locked with our own tailer paused -- the game itself
            # has it open, which only happens if the server's running.
            server_running = game_is_running(self._proc)
            if not server_running:
                self._print(f"Couldn't archive log: {result}", "err")
                return

            if not messagebox.askyesno("Archive Log",
                    "The server is running and has the log file open, so it "
                    "needs to briefly stop and restart to archive it. "
                    "Connected players will be disconnected and can rejoin "
                    "once it's back.\n\nContinue?", parent=self):
                return

            self._print("Archive Log: stopping the server to free the log file…", "warn")

            def do_archive_then_restart():
                ok2, result2, _locked2 = archive_log_now(GAME_LOG_PATH)
                if ok2:
                    self._print(f"Log archived to {result2}", "ok")
                else:
                    self._print(f"Couldn't archive log even after stopping the server: {result2}", "err")
                self._start()

            # _stop() clears self._proc right away, so grab it first.
            proc_to_wait = self._proc
            self._stop(reboot=False)

            def wait_for_exit_then_archive():
                if proc_to_wait is not None:
                    try:
                        proc_to_wait.wait(timeout=5)
                    except Exception:
                        pass
                self.after(500, do_archive_then_restart)
            threading.Thread(target=wait_for_exit_then_archive, daemon=True).start()

        self._archive_log_with_tailer_paused(after_pausing_tailer)

    def _start_auto_archive_check(self):
        # Backup for the game's own size-based archiving, which doesn't
        # always seem to fire reliably on a long-running server. Never
        # restarts the server on its own -- only archives when it can do
        # so without kicking anyone; the manual button covers the rest.
        try:
            if should_auto_archive(GAME_LOG_PATH):
                def after_pausing_tailer(ok, result, locked):
                    if ok:
                        self._print(f"Log passed the size threshold — auto-archived to {result}", "warn")
                    elif not locked:
                        self._print(f"Log passed the size threshold, but auto-archive failed: {result}", "err")
                self._archive_log_with_tailer_paused(after_pausing_tailer)
        except Exception:
            pass
        self.after(60000, self._start_auto_archive_check)

    def _append_log(self, line, tag):
        self.log.config(state="normal")
        self.log.insert("end", line+"\n", tag)
        if float(self.log.index("end-1c").split(".")[0]) > 5000:
            self.log.delete("1.0","1000.0")
        self.log.see("end"); self.log.config(state="disabled")

    def _start(self):
        exe = self.v_exe.get().strip()
        if not exe or not os.path.isfile(exe):
            messagebox.showerror("Not found",
                "Could not find the game.\nPlease browse first.", parent=self)
            return
        # The archive below runs on a worker thread for up to a couple of
        # seconds before the server is actually launched, so Start has to go
        # grey now - not when _set_running fires - or a second click in that
        # window launches a second server.
        if self._starting:
            return
        self._starting = True
        self._btn_start.config(state="disabled")
        # Archive the previous log before starting -- also covers auto-reboot,
        # since that calls _start() too.
        self._archive_log_with_tailer_paused(
            lambda ok, result, locked: self._on_pre_start_archive_done(ok, result, locked, exe))

    def _on_pre_start_archive_done(self, ok, result, locked, exe):
        if ok:
            self._print(f"Log archived to {result}", "ok")
        elif not locked and result != "No active log file found to archive.":
            self._print(f"Couldn't archive previous log: {result}", "err")
        try:
            self._start_after_archive(exe)
        finally:
            self._starting = False
            # A launch that failed never reached _set_running(True), which
            # is what would otherwise own the button from here.
            if not game_is_running(self._proc):
                self._btn_start.config(state="normal")

    def _start_after_archive(self, exe):
        try: port = int(self.v_port.get())
        except: port = 1757
        self._save()
        access, refresh, identity = build_server_tokens()
        console_token = build_console_token()
        try:
            with open(CONSOLE_TOKEN_FILE,"w") as f:
                f.write(console_token)
        except: pass
        self._served_game_dir = os.path.dirname(exe)
        if not self._auth_on:
            start_auth_service(self._print, lambda: self._served_game_dir)
            self._auth_on = True
        args = [exe, "/force_offline",
                "/access_token", access, "/refresh_token", refresh,
                "/identity_token", identity]
        if not self.v_show_game.get():
            args += ["-batchmode", "-nographics"]
        args += ["/fly", "/launcherauth", "/start_server", "-1", "false", str(port)]
        if self.v_debug_helper.get():
            args.append("/debug_helper")
        if self.v_tutorial_scene.get():
            args.append("/tutorialScene")
        elif self.v_quest_scene.get():
            args.append("/questScene")
        self._warn_mods_outside_scene(os.path.dirname(exe))
        self._print(f"Opening server on port {port}…", "warn")
        try:
            # Whether MelonLoader's console shows up is controlled by the
            # Show MelonLoader toggle: hiding it means redirecting the
            # child's std handles at creation time (skips AllocConsole);
            # showing it means leaving them alone.
            kwargs = {} if self.v_show_melonloader.get() else \
                     {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
            self._proc = subprocess.Popen(args, cwd=os.path.dirname(exe), **kwargs)
        except Exception as e:
            self._print(f"Failed: {e}", "err"); return
        self._set_running(True)
        self._print(f"Server running (PID {self._proc.pid})", "ok")
        threading.Thread(target=self._watch, daemon=True).start()
        # Kill a still-pending reboot timer from before this start
        if getattr(self, "_reboot_after_id", None):
            self.after_cancel(self._reboot_after_id)
        self._reboot_after_id = None
        self._schedule_auto_reboot()
        self._schedule_backups()

    def _watch(self):
        time.sleep(8)
        if game_is_running(self._proc):
            self._print("Server ready. Players may connect.", "ok")
        else:
            self._print("Server exited unexpectedly.", "err")
            self.after(0, lambda: self._set_running(False))

    def _stop(self, reboot=False):
        # A manual stop
        if getattr(self, "_reboot_after_id", None):
            self.after_cancel(self._reboot_after_id)
            self._reboot_after_id = None
        self._cancel_backups()
        if self._proc:
            try: self._proc.terminate(); self._print("Closing server…", "warn")
            except Exception as e: self._print(f"Stop failed: {e}", "err")
        self._set_running(False); self._proc = None
        if reboot:
            self._print("Auto-reboot: restarting in 10 seconds…", "warn")
            self.after(10000, self._start)

    def _schedule_auto_reboot(self):
        """Called once after server starts. Cancels any existing schedule,
        reads current settings, and sets up the next reboot alarm."""
        if hasattr(self, "_reboot_after_id") and self._reboot_after_id:
            self.after_cancel(self._reboot_after_id)
            self._reboot_after_id = None
        ss = load_server_settings()
        if not ss.get("auto_reboot_enabled", False):
            return
        mode = ss.get("auto_reboot_mode", "time")
        if mode == "interval":
            hours = max(1, ss.get("auto_reboot_interval", 6))
            ms = hours * 3600 * 1000
            self._print(f"Auto-reboot scheduled in {hours}h.", "dim")
        else:
            import datetime
            now = datetime.datetime.now()
            target_hour   = max(0, min(23, ss.get("auto_reboot_hour", 4)))
            target_minute = max(0, min(59, ss.get("auto_reboot_minute", 0)))
            target = now.replace(hour=target_hour, minute=target_minute,
                                 second=0, microsecond=0)
            if target <= now:
                target += datetime.timedelta(days=1)
            ms = int((target - now).total_seconds() * 1000)
            self._print(f"Auto-reboot scheduled for {target.strftime('%H:%M')}.", "dim")
        self._reboot_after_id = self.after(ms, self._do_auto_reboot)

    def _do_auto_reboot(self):
        self._reboot_after_id = None
        if game_is_running(self._proc):
            self._print("Auto-reboot triggered.", "warn")
            self._stop(reboot=True)
        else:
            # Server already stopped — just reschedule for next time
            self._schedule_auto_reboot()

    BACKUP_CHECK_MS = 60 * 1000

    def _schedule_backups(self):
        """Called when the server starts. Once a minute while it runs, checks
        whether a backup is due. Settings are re-read each time, so changing
        them in Advanced Settings takes effect without restarting the server."""
        self._cancel_backups()
        ss = load_server_settings()
        hours = ss.get("server_backup_time", 0)
        if hours > 0:
            keep = ss.get("server_backup_max_backups", 0)
            self._print(f"Automatic backups every {hours:g}h"
                        + (f", keeping the newest {keep}." if keep else "."), "dim")
        self._backup_job = self.after(self.BACKUP_CHECK_MS, self._backup_tick)

    def _cancel_backups(self):
        job = getattr(self, "_backup_job", None)
        if job:
            try: self.after_cancel(job)
            except Exception: pass
        self._backup_job = None

    def _backup_tick(self):
        self._backup_job = None
        if not (self._proc and self._proc.poll() is None):
            return  # the server has stopped; nothing to back up on a timer
        ss = load_server_settings()
        hours = ss.get("server_backup_time", 0)
        if hours > 0 and not getattr(self, "_backup_busy", False) \
                and backup_due(SERVER_DATA_DIR, hours):
            self._backup_busy = True
            threading.Thread(target=self._do_backup,
                             args=(ss.get("server_backup_max_backups", 0),),
                             daemon=True).start()
        self._backup_job = self.after(self.BACKUP_CHECK_MS, self._backup_tick)

    def _do_backup(self, max_backups):
        try:
            path, added, skipped = create_backup(SERVER_DATA_DIR)
            size = os.path.getsize(path)
            size_text = f"{size / (1024 * 1024):.1f} MB" if size >= 1024 * 1024 else f"{max(1, size // 1024)} KB"
            self._print(f"Backup saved: {os.path.basename(path)} ({size_text}, {added} files)", "ok")
            if skipped:
                self._print(f"{len(skipped)} file(s) were in use and left out of the backup.", "warn")
            removed = prune_backups(SERVER_DATA_DIR, max_backups)
            if removed:
                self._print(f"Removed {len(removed)} old backup(s) (keeping {max_backups}).", "dim")
        except Exception as e:
            self._print(f"Backup failed: {e}", "err")
        finally:
            self._backup_busy = False

    def _set_running(self, on):
        def _do():
            self._dot.itemconfig("dot", fill=GREEN if on else MUTED)
            self._status_var.set("Online" if on else "Offline")
            self._pid_var.set(f"PID {self._proc.pid}" if on and self._proc else "")
            self._btn_start.config(state="disabled" if on else "normal")
            self._btn_stop.config(state="normal" if on else "disabled")
        self.after(0, _do)

    def _on_close(self):
        if game_is_running(self._proc):
            if messagebox.askyesno("Server running",
                                   "Stop the server before closing?", parent=self):
                self._stop()
        self.destroy()


from server.core.header_registry import register_header_button
register_header_button("discord", "💬 Discord", "_open_discord", built_in=True)
register_header_button("copy_token", "📋 Copy Console Token", "_copy_console_token", built_in=True)
