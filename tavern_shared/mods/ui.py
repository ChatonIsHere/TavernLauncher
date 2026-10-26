"""The Mod Manager window and the Community Mods, Mod Sources and Change
Version windows it opens, shared by both launchers."""
import json
import os
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog

from tavern_shared.bg import post, run_bg
from tavern_shared.game_guard import confirm_while_game_running
from tavern_shared.mod_install import _melonloader_installed, _tavernlib_installed
from tavern_shared.theme import (
    AMBER, AMBERDIM, BG, BORDER, CYAN, GREEN, MUTED, PARCH, RED, SURF, _btn,
    _mk_tree, _scroll_stack, _window_header,
)
from tavern_shared.window_chrome import _enable_dark_titlebar

from tavern_shared.mods.base import ModManagerError, _parse_version, load_cfg
from tavern_shared.mods.cache import clear_mod_cache, ensure_mod_libraries
from tavern_shared.mods.install import (
    _is_pre_v2, _verify_memo, disable_mod, disable_untracked_dll, enable_mod,
    enable_untracked_dll, has_target, install_mod_closure, installed_targets,
    list_installed_mods, list_mods, list_untracked_mods, mod_status,
    uninstall_mod,
)
from tavern_shared.mods.manifest import runs_in_scene
from tavern_shared.mods.modlist import (
    apply_import, export_modlist, import_modlist, modlist_import_disables,
)
from tavern_shared.mods.pins import _parse_pin_entry, add_pin, list_pinned, remove_pin
from tavern_shared.mods.repos import (
    _is_default, _source_display, add_repo, fetch_indexes, list_repos, remove_repo,
)
from tavern_shared.mods.resolve import _fetch_manifest, resolve_mod_by_id


def record_matches_target(rec, target):
    """Whether this launcher lists the record: it holds `target`, or predates
    v2 (listed as damaged, so a reinstall moves it to the per-target layout)."""
    return has_target(rec, target) or _is_pre_v2(rec)


_TARGET_NAMES = {"pc": "PC", "server": "Server", "quest": "Quest"}


def target_label(targets):
    """"PC + Server", "Server only", ..."""
    names = [_TARGET_NAMES.get(t, t) for t in sorted(targets, key=list(_TARGET_NAMES).index)
             if t in _TARGET_NAMES]
    if not names:
        return "-"
    return names[0] + " only" if len(names) == 1 else " + ".join(names)


def source_label(source_repo):
    return "Modding Tavern" if _is_default(source_repo) else _source_display(source_repo)


class ModListWindow(tk.Toplevel):
    """What the Mod Manager and Community Mods windows share. A subclass
    builds _refresh_btn and _status in _build and provides _load, _state_ids,
    _apply_states, _read_installed and _apply_installed."""

    def __init__(self, parent, title, size, minsize, game_dir, target, on_change,
                 is_game_running, server_scene):
        super().__init__(parent)
        self.title(title)
        self.configure(bg=BG)
        self.geometry(size)
        self.minsize(*minsize)
        self._game_dir = game_dir
        self._target = target
        self._on_change = on_change
        self._is_game_running = is_game_running     # see game_guard
        self._server_scene = server_scene           # server only: () -> its scene
        self._index = []
        self._rows = {}
        self._busy = False
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        _enable_dark_titlebar(self)
        self.transient(parent)
        self._load(force=False)

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
        """After an action that changed Mods/."""
        self._set_busy(False, msg)
        def reread(data):
            self._apply_installed(data)
            self._refresh_states()
        run_bg(self, self._read_installed, on_ok=reread, on_err=lambda e: None)
        if self._on_change:
            self._on_change()

    def _install(self, mod, version=None):
        """Installs `mod`'s closure at `version`, default its highest. The
        game-running guard comes last, right before the write it protects:
        accepting its risk for an install that is then refused anyway teaches
        players to click through it."""
        if self._busy:
            return
        if not _melonloader_installed(self._game_dir):
            messagebox.showwarning("Install MelonLoader first",
                f"{mod.name} is a MelonLoader mod. Install MelonLoader from the "
                "Setup window first.", parent=self)
            return
        if not _tavernlib_installed(self._game_dir):
            messagebox.showwarning("Install TavernLib first",
                f"{mod.name} needs TavernLib. Install it from the Setup window "
                "first.", parent=self)
            return
        # Asked, not refused: the operator may be about to change the scene.
        server_scene = self._server_scene() if self._server_scene else None
        if not runs_in_scene(mod.scenes, server_scene):
            if not messagebox.askyesno("Not made for this scene",
                    f"{mod.name} says it works in the {', '.join(mod.scenes)} "
                    f"scene{'s' if len(mod.scenes) > 1 else ''}, and this server is set "
                    f"to run the {server_scene} scene.\n\nInstall it anyway?",
                    parent=self):
                return
        if not confirm_while_game_running(self, self._is_game_running,
                                          f"Installing {mod.name}"):
            return
        label = f"{mod.name} {version}" if version else mod.name
        self._set_busy(True, f"Installing {label}...")
        index, game_dir, target = self._index, self._game_dir, self._target
        def install():
            install_mod_closure(game_dir, mod, index, list_repos(load_cfg()),
                                lambda m: post(self, self._status.set, m),
                                target, version=version)
        run_bg(self, install,
               on_ok=lambda _: self._finish(f"{label} installed."),
               on_err=lambda e: self._finish(f"Install failed: {e}"))


# (id, version) -> "Requires: ..." text, kept across windows.
_DEPS_CACHE = {}


class CommunityModsWindow(ModListWindow):
    """Browse the merged index and install from it, as cards with a search box."""

    _COL_LABELS = ("Status", "Author", "Mod", "Installed", "Latest", "Source")
    _WIDTHS     = (90,       110,      160,   74,          64,       110)
    _STATE_STYLE = {    # state -> (status, its colour, button)
        "missing":  ("Available",    PARCH, "Install"),
        "damaged":  ("Damaged",      RED,   "Reinstall"),
        "current":  ("Installed",    GREEN, "Reinstall"),
        "outdated": ("Update ready", AMBER, "Update"),
        "unknown":  ("Installed",    MUTED, "Reinstall"),
    }

    def __init__(self, parent, game_dir, target, on_change, is_game_running, server_scene):
        self._repos = []
        self._visible_ids = []
        self._card_widgets = {}     # id -> widgets updated in place
        self._deps_pending = set()
        self._filter_job = None
        self._sources_win = None
        super().__init__(parent, "Community Mods", "860x580", (760, 440), game_dir,
                         target, on_change, is_game_running, server_scene)

    def _build(self):
        h = _window_header(self, "Community Mods")
        _btn(h, "Manage Sources", self._open_sources, style="dim",
             font=("Segoe UI",9), pady=4, padx=10).pack(side="right", padx=12)

        desc = ("Mods available from your configured sources. Sources other than "
                "Modding Tavern are not vetted; you install from them at your "
                "own risk.")
        tk.Label(self, text=desc, bg=BG, fg=MUTED, font=("Segoe UI",9),
                 wraplength=800, justify="left"
        ).pack(anchor="w", padx=16, pady=(10,6))

        sf = tk.Frame(self, bg=BG)
        sf.pack(fill="x", padx=16, pady=(0,8))
        tk.Label(sf, text="🔍", bg=BG, fg=MUTED, font=("Segoe UI",10)).pack(side="left")
        self.v_search = tk.StringVar(value="")
        self.v_search.trace_add("write", lambda *_: self._schedule_filter())
        tk.Entry(sf, textvariable=self.v_search, bg=SURF, fg=PARCH,
                 insertbackground=AMBER, relief="flat", font=("Consolas",10),
                 bd=6).pack(side="left", fill="x", expand=True, padx=(6,0))

        list_wrap = tk.Frame(self, bg=BG)
        list_wrap.pack(fill="both", expand=True, padx=16)

        header = tk.Frame(list_wrap, bg=SURF)
        header.pack(fill="x")
        for text, width in zip(self._COL_LABELS, self._WIDTHS):
            cell = tk.Frame(header, bg=SURF, width=width, height=24)
            cell.pack(side="left", fill="y"); cell.pack_propagate(False)
            tk.Label(cell, text=text, bg=SURF, fg=AMBER, font=("Segoe UI",8,"bold"),
                     anchor="w").pack(fill="both", padx=6)
        self._cards_frame = _scroll_stack(list_wrap)

        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=16, pady=(8,4))
        self._refresh_btn = _btn(bar, "Refresh", lambda: self._load(force=True),
                                 style="dim", font=("Segoe UI",9), pady=5, padx=12)
        self._refresh_btn.pack(side="right")
        if self._target == "pc":
            _btn(bar, "Clear Mod Cache", self._on_clear_cache, style="dim",
                 font=("Segoe UI",9), pady=5, padx=12).pack(side="right", padx=(0,8))

        self._status = tk.StringVar(value="Loading community mods...")
        tk.Label(self, textvariable=self._status, bg=BG, fg=CYAN,
                 font=("Segoe UI",9), wraplength=800, justify="left"
        ).pack(anchor="w", padx=16, pady=(10,10))

    def _load(self, force=False):
        self._set_busy(True, "Loading community mods...")
        game_dir, target = self._game_dir, self._target
        def load():
            repos = list_repos(load_cfg())
            index = fetch_indexes(repos, force=force)
            return repos, index, list_mods(index, target), list_installed_mods(game_dir)
        run_bg(self, load, on_ok=lambda r: self._render(*r),
               on_err=lambda e: self._set_busy(False, f"Couldn't load community mods: {e}"))

    def _render(self, repos, index, available, installed):
        self._repos, self._index = repos, index
        installed_by_id = self._installed_versions(installed)
        self._rows = {s.id: {
            "id": s.id, "name": s.name, "author": s.author or "",
            "description": s.description or "",
            "latest": s.highest(), "summary": s,
            "installed": installed_by_id.get(s.id), "state": "missing",
        } for s in available}
        for child in self._cards_frame.winfo_children():
            child.destroy()
        self._card_widgets = {}
        self._visible_ids = []
        for mod_id in self._sorted_ids():
            self._mk_card(mod_id)
        self._apply_filter()
        self._set_busy(False, "")
        self._refresh_states()

    def _installed_versions(self, installed):
        return {m["id"]: m.get("version", "?") for m in installed
                if m.get("id") and record_matches_target(m, self._target)}

    def _sorted_ids(self):
        return sorted(self._rows, key=lambda i: self._rows[i]["name"].lower())

    def _schedule_filter(self):
        if self._filter_job:
            self.after_cancel(self._filter_job)
        self._filter_job = self.after(150, self._apply_filter)

    def _apply_filter(self):
        """Shows the matching cards in name order. A re-packed card lands at
        the end, so when the set changes all of them go and come back."""
        self._filter_job = None
        query = self.v_search.get().strip().lower()
        def matches(mod_id):
            r = self._rows[mod_id]
            return query in f"{r['name']} {r['author']} {r['id']} {r['description']}".lower()
        visible = [mid for mid in self._sorted_ids() if not query or matches(mid)]
        if visible != self._visible_ids:
            for w in self._card_widgets.values():
                w["card"].pack_forget()
            for mod_id in visible:
                self._card_widgets[mod_id]["card"].pack(fill="x", pady=(0,6))
            self._visible_ids = visible

        if query and self._rows:
            self._status.set(f"{len(visible)} of {len(self._rows)} mod(s) match '{query}'.")
        self._queue_dep_fetches()

    def _mk_card(self, mod_id):
        r = self._rows[mod_id]
        s = r["summary"]
        card = tk.Frame(self._cards_frame, bg=SURF,
                        highlightbackground=BORDER, highlightthickness=1)

        top = tk.Frame(card, bg=SURF)
        top.pack(fill="x", pady=(8,0))
        cells = []
        for width in self._WIDTHS:
            cell = tk.Frame(top, bg=SURF, width=width, height=26)
            cell.pack(side="left", fill="y"); cell.pack_propagate(False)
            cells.append(cell)

        status_lbl = tk.Label(cells[0], bg=SURF, font=("Segoe UI",9), anchor="w")
        status_lbl.pack(fill="both", padx=6)
        installed_var = tk.StringVar(value=r["installed"] or "-")
        for cell, text, bold in ((cells[1], r["author"] or "-", False),
                                 (cells[2], r["name"], True),
                                 (cells[3], installed_var, False),
                                 (cells[4], r["latest"], False),
                                 (cells[5], source_label(s.source_repo), False)):
            opts = {"textvariable": text} if isinstance(text, tk.StringVar) else {"text": text}
            tk.Label(cell, **opts, bg=SURF, fg=PARCH, anchor="w",
                     font=("Segoe UI",9,"bold") if bold else ("Segoe UI",9)
            ).pack(fill="both", padx=6)

        btn = _btn(top, "Install", lambda: self._install(s),
                   style="primary", font=("Segoe UI",9), pady=4, padx=10, width=11)
        btn.pack(side="right", padx=(6,10))

        parity_text = "Exact version required" if s.parity_required else "Any compatible version"
        tag_text = f"{target_label(s.targets)}  ·  {parity_text}"
        if s.scenes:
            tag_text += "  ·  " + ", ".join(x.capitalize() for x in s.scenes) + " scene" \
                + ("s" if len(s.scenes) > 1 else "") + " only"
        tk.Label(card, text=tag_text, bg=SURF, fg=CYAN,
                 font=("Segoe UI",8), anchor="w"
        ).pack(fill="x", padx=10, pady=(4,2))

        tk.Label(card, text=r["description"] or "No description provided.",
                 bg=SURF, fg=PARCH, font=("Segoe UI",8), wraplength=760,
                 justify="left", anchor="w"
        ).pack(fill="x", padx=10, pady=(0,2))

        requires_var = tk.StringVar(value="Loading dependencies…")
        tk.Label(card, textvariable=requires_var, bg=SURF, fg=PARCH,
                 font=("Segoe UI",8,"italic"), wraplength=760, justify="left", anchor="w"
        ).pack(fill="x", padx=10, pady=(0,8))

        self._card_widgets[mod_id] = {
            "card": card, "status_lbl": status_lbl, "installed_var": installed_var,
            "btn": btn, "requires_var": requires_var,
        }
        self._apply_card_state(mod_id)

    def _apply_card_state(self, mod_id):
        word, color, label = self._STATE_STYLE[self._rows[mod_id]["state"]]
        w = self._card_widgets[mod_id]
        w["status_lbl"].config(text=word, fg=color)
        w["btn"].config(text=label, state="disabled" if self._busy else "normal")

    def _busy_changed(self):
        for mod_id in self._card_widgets:
            self._apply_card_state(mod_id)

    def _state_ids(self):
        return list(self._rows)

    def _apply_states(self, states):
        for mod_id, st in states.items():
            if mod_id in self._rows:
                self._rows[mod_id]["state"] = st
        self._busy_changed()

    def _read_installed(self):
        return list_installed_mods(self._game_dir)

    def _apply_installed(self, installed):
        versions = self._installed_versions(installed)
        for mod_id, r in self._rows.items():
            r["installed"] = versions.get(mod_id)
            self._card_widgets[mod_id]["installed_var"].set(r["installed"] or "-")

    def _queue_dep_fetches(self):
        to_fetch = []
        for mod_id in self._visible_ids:
            r = self._rows[mod_id]
            key = (mod_id, r["latest"])
            if key in _DEPS_CACHE:
                self._card_widgets[mod_id]["requires_var"].set(_DEPS_CACHE[key])
            elif key not in self._deps_pending:
                self._deps_pending.add(key)
                to_fetch.append((mod_id, key, r["summary"]))
        if not to_fetch:
            return
        repos = self._repos
        def fetch():
            for mod_id, key, summary in to_fetch:
                try:
                    deps = resolve_mod_by_id(repos, mod_id, prefer_repo=summary.source_repo).dependencies
                    text = ("Requires: " + ", ".join(f"{d}>={v}" for d, v in sorted(deps.items()))
                            if deps else "No dependencies.")
                except ModManagerError:
                    text = "Couldn't load dependency info."
                _DEPS_CACHE[key] = text
                post(self, self._deps_fetched, key, mod_id, text)
        run_bg(self, fetch)

    def _deps_fetched(self, key, mod_id, text):
        self._deps_pending.discard(key)
        w = self._card_widgets.get(mod_id)
        if w:
            w["requires_var"].set(text)

    def _on_clear_cache(self):
        if self._busy:
            return
        if not messagebox.askyesno("Clear mod cache",
                "Delete every cached mod and library version on this machine?\n\n"
                "Nothing currently installed in Mods/ is touched - this only clears "
                "the local copies kept so switching servers doesn't re-download. "
                "The next install or server join will fetch fresh from source.",
                parent=self):
            return
        self._set_busy(True, "Clearing mod cache...")
        run_bg(self, clear_mod_cache,
               on_ok=lambda _: self._set_busy(False, "Mod cache cleared."),
               on_err=lambda e: self._set_busy(False, f"Clear cache failed: {e}"))

    def _open_sources(self):
        if self._busy:
            return
        if self._sources_win and self._sources_win.winfo_exists():
            self._sources_win.lift(); return
        self._sources_win = ModSourcesWindow(self, on_change=lambda: self._load(force=True))


class ModSourcesWindow(tk.Toplevel):
    def __init__(self, parent, on_change):
        super().__init__(parent)
        self.title("Mod Sources")
        self.configure(bg=BG)
        self.geometry("520x360")
        self.resizable(False, False)
        self._on_change = on_change
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        _enable_dark_titlebar(self)
        self.transient(parent)

    def _build(self):
        _window_header(self, "Mod Sources")

        tk.Label(self,
            text="Repositories the launcher fetches community mods from. The "
                 "Modding Tavern source is always present. You can add your own "
                 "sources, but they are not checked by Modding Tavern; you are "
                 "responsible for what you install from them.",
            bg=BG, fg=MUTED, font=("Segoe UI",9), wraplength=470, justify="left"
        ).pack(anchor="w", padx=20, pady=(10,6))

        lb_frame = tk.Frame(self, bg=BORDER, highlightbackground=BORDER, highlightthickness=1)
        lb_frame.pack(fill="both", expand=True, padx=20, pady=(0,8))
        self._listbox = tk.Listbox(lb_frame, bg=SURF, fg=PARCH, bd=0,
                                   highlightthickness=0, selectbackground=AMBERDIM,
                                   font=("Consolas",9), activestyle="none")
        self._listbox.pack(fill="both", expand=True, padx=1, pady=1)

        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=20, pady=(0,12))
        _btn(bar, "+ Add source", self._on_add, style="primary",
             font=("Segoe UI",9), pady=5, padx=12).pack(side="left")
        _btn(bar, "- Remove selected", self._on_remove, style="danger",
             font=("Segoe UI",9), pady=5, padx=12).pack(side="left", padx=8)

        self._status = tk.StringVar(value="")
        tk.Label(self, textvariable=self._status, bg=BG, fg=CYAN,
                 font=("Segoe UI",8), wraplength=470, justify="left"
        ).pack(anchor="w", padx=20, pady=(0,8))
        self._reload()

    def _reload(self):
        self._repos = list_repos(load_cfg())
        self._listbox.delete(0, tk.END)
        for url in self._repos:
            tag = "  (default, always on)" if _is_default(url) else ""
            self._listbox.insert(tk.END, _source_display(url) + tag)

    def _on_add(self):
        url = simpledialog.askstring("Add mod source",
            "Raw-content base URL of the repository\n"
            "(e.g. https://raw.githubusercontent.com/user/repo/main):",
            parent=self)
        if not url:
            return
        if not messagebox.askokcancel("Unverified source",
                "This source has NOT been checked by Modding Tavern.\n\n"
                "Mods you install from it run with full access to your game and "
                "PC. You are responsible for vetting what you install.\n\n"
                "Add this source anyway?", parent=self):
            return
        self._status.set("Checking source...")
        run_bg(self, lambda: add_repo(load_cfg(), url),
               on_ok=lambda _: self._changed("Source added."),
               on_err=lambda e: self._status.set(f"Couldn't add source: {e}"))

    def _changed(self, msg):
        self._status.set(msg)
        self._reload()
        self._on_change()

    def _on_remove(self):
        sel = self._listbox.curselection()
        if not sel:
            return
        url = self._repos[sel[0]]
        if _is_default(url):
            messagebox.showinfo("Can't remove",
                "The Modding Tavern source can't be removed.", parent=self)
            return
        if not messagebox.askyesno("Remove source",
                f"Remove this source?\n\n{url}", parent=self):
            return
        try:
            remove_repo(load_cfg(), url)
        except Exception as e:
            self._status.set(f"Couldn't remove: {e}")
            return
        self._changed("Source removed.")


class ModManagerWindow(ModListWindow):
    """Everything in Mods/, managed and untracked, and the actions on it.
    Untracked rows only toggle: no uninstall or version change for files this
    manager didn't place."""

    _COLS   = ("status", "author", "mod", "installed", "latest", "managed", "source")
    _WIDTHS = (90,       110,      160,   74,          70,       80,        120)
    _STATE_WORD = {
        "missing":  "Missing",
        "damaged":  "Damaged",
        "current":  "Up to date",
        "outdated": "Update ready",
        "unknown":  "Installed",
    }

    def __init__(self, parent, game_dir, target="pc", on_change=None,
                 is_game_running=None, server_scene=None):
        self._community_win = None
        self._pinned_ids = set()    # client only: pins are a join-time concept
        super().__init__(parent, "Mod Manager", "800x480", (680, 380), game_dir,
                         target, on_change, is_game_running, server_scene)

    def _build(self):
        h = _window_header(self, "Mod Manager")
        for text, cmd, padx in (("Community Mods", self._open_community, 12),
                                ("Import Modlist", self._on_import_modlist, (12,0)),
                                ("Export Modlist", self._on_export_modlist, (12,0))):
            _btn(h, text, cmd, style="dim", font=("Segoe UI",9), pady=4, padx=10
                 ).pack(side="right", padx=padx)

        desc = ("Every mod installed on this machine - the ones this launcher "
                 "manages plus anything dropped into Mods/ by hand (marked "
                 "\"Untracked\"). Select one, then enable/disable, remove, or "
                 "(managed mods only) change its version, or right-click for the "
                 "same options. To add something new, use Community Mods.")
        tk.Label(self, text=desc, bg=BG, fg=MUTED, font=("Segoe UI",9),
                 wraplength=760, justify="left"
        ).pack(anchor="w", padx=16, pady=(10,6))

        table_wrap = tk.Frame(self, bg=BG)
        table_wrap.pack(fill="both", expand=True, padx=16)
        self._tree = _mk_tree(table_wrap, self._COLS, self._WIDTHS, height=10)
        for tag, color in (("missing", PARCH), ("damaged", RED), ("current", GREEN),
                           ("outdated", AMBER), ("unknown", MUTED),
                           ("untracked", PARCH), ("disabled", MUTED)):
            self._tree.tag_configure(tag, foreground=color)
        self._tree.bind("<<TreeviewSelect>>", lambda e: self._sync_buttons())
        self._tree.bind("<Button-3>", self._on_right_click)

        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=16, pady=(8,4))
        self._uninstall_btn = _btn(bar, "Uninstall", self._on_uninstall, style="danger",
                                   font=("Segoe UI",9), pady=5, padx=14)
        self._uninstall_btn.pack(side="left")
        self._toggle_btn = _btn(bar, "Disable", self._on_toggle, style="normal",
                                font=("Segoe UI",9), pady=5, padx=14)
        self._toggle_btn.pack(side="left", padx=8)
        self._refresh_btn = _btn(bar, "Refresh", lambda: self._load(force=True),
                                 style="dim", font=("Segoe UI",9), pady=5, padx=12)
        self._refresh_btn.pack(side="right")

        self._status = tk.StringVar(value="Loading installed mods...")
        tk.Label(self, textvariable=self._status, bg=BG, fg=CYAN,
                 font=("Segoe UI",9), wraplength=760, justify="left"
        ).pack(anchor="w", padx=16, pady=(10,10))
        self._sync_buttons()

    def _load(self, force=False):
        self._set_busy(True, "Loading installed mods...")
        if force:
            # Refresh is the user saying "look again", so re-hash everything.
            _verify_memo.clear()
        prev_index = self._index
        def load():
            on_disk = self._read_installed()
            # The index only adds context, so a failed fetch keeps the old one.
            try:
                index = fetch_indexes(list_repos(load_cfg()), force=force)
            except Exception:
                index = prev_index
            return index, on_disk
        run_bg(self, load, on_ok=lambda r: self._render(*r),
               on_err=lambda e: self._set_busy(False, f"Couldn't read installed mods: {e}"))

    def _render(self, index, on_disk):
        self._index = index
        if self._target == "pc":
            self._pinned_ids = {_parse_pin_entry(p)[0] for p in list_pinned(load_cfg())}
        self._apply_installed(on_disk)
        self._set_busy(False, "")
        self._refresh_states()

    def _read_installed(self):
        return list_installed_mods(self._game_dir), list_untracked_mods(self._game_dir)

    def _state_ids(self):
        return [r["key"] for r in self._rows.values() if r["kind"] == "managed"]

    def _busy_changed(self):
        self._sync_buttons()

    def _apply_installed(self, on_disk):
        installed, untracked = on_disk
        summaries = {}     # id -> its highest-major ModSummary
        for s in self._index:
            cur = summaries.get(s.id)
            if cur is None or _parse_version(s.highest()) > _parse_version(cur.highest()):
                summaries[s.id] = s
        rows = {}
        for meta in installed:
            mod_id = meta.get("id")
            if not mod_id or not record_matches_target(meta, self._target):
                continue
            s = summaries.get(mod_id)
            rows[f"m:{mod_id}"] = {
                "kind": "managed", "key": mod_id,
                "name": s.name if s else mod_id,
                "author": s.author if s else "",
                "installed": meta.get("version", "?"),
                "latest": s.highest() if s else "-",
                "managed": "Managed",
                "source": source_label(s.source_repo) if s else "-",
                "summary": s, "state": "unknown",
                "enabled": meta.get("enabled", True),
                "targets": installed_targets(meta),
            }
        for u in untracked:
            rows[f"u:{u['kind']}:{u['name']}"] = {
                "kind": f"untracked_{u['kind']}", "key": u["name"],
                "name": u["name"], "author": "", "installed": "-", "latest": "-",
                "managed": "Untracked", "source": "-",
                "summary": None, "state": None, "enabled": u["enabled"],
            }
        self._rows = rows

        keep = self._selected_id()
        self._tree.delete(*self._tree.get_children())
        for row_id in sorted(self._rows, key=lambda i: self._rows[i]["name"].lower()):
            self._tree.insert("", "end", iid=row_id, values=self._row_values(row_id),
                              tags=(self._row_tag(self._rows[row_id]),))
        if keep and self._tree.exists(keep):
            self._tree.selection_set(keep)
        self._sync_buttons()

    def _row_tag(self, r):
        if r.get("enabled") is False:
            return "disabled"
        if r["kind"] != "managed":
            return "untracked"
        return r["state"] or "current"

    def _row_values(self, row_id):
        r = self._rows[row_id]
        if r["kind"] != "managed":
            status = "Enabled" if r["enabled"] else "Disabled"
        elif r.get("enabled") is False:
            status = "Disabled"
        else:
            status = self._STATE_WORD.get(r["state"], "")
        return (status, r["author"], r["name"],
                r["installed"], r["latest"], r["managed"], r["source"])

    def _apply_states(self, states):
        for row_id, r in self._rows.items():
            if r["kind"] == "managed" and r["key"] in states:
                r["state"] = states[r["key"]]
                if self._tree.exists(row_id):
                    self._tree.item(row_id, values=self._row_values(row_id),
                                    tags=(self._row_tag(r),))
        self._sync_buttons()

    def _selected_id(self):
        sel = self._tree.selection()
        return sel[0] if sel else None

    def _selected_row(self):
        sel = self._selected_id()
        return self._rows.get(sel) if sel else None

    def _sync_buttons(self):
        r = self._selected_row()
        if self._busy or r is None:
            self._uninstall_btn.config(state="disabled")
            self._toggle_btn.config(state="disabled")
            return
        self._uninstall_btn.config(state="normal" if r["kind"] == "managed" else "disabled")
        self._toggle_btn.config(text="Enable" if not r["enabled"] else "Disable", state="normal")

    def _on_right_click(self, event):
        row = self._tree.identify_row(event.y)
        if not row:
            return
        self._tree.selection_set(row)
        self._sync_buttons()
        r = self._rows.get(row)
        if not r:
            return
        menu = tk.Menu(self, tearoff=0, bg=SURF, fg=PARCH,
                       activebackground=AMBERDIM, activeforeground=PARCH)
        managed = r["kind"] == "managed"
        menu.add_command(label="Uninstall", command=self._on_uninstall,
                         state="normal" if managed else "disabled")
        menu.add_command(label="Enable" if not r["enabled"] else "Disable",
                         command=self._on_toggle)
        if managed and self._target == "pc":
            menu.add_separator()
            # Kept on self: a BooleanVar with no Python reference can be
            # collected while the menu is still open.
            self._pin_var = tk.BooleanVar(value=r["key"] in self._pinned_ids)
            menu.add_checkbutton(label="Keep Enabled", variable=self._pin_var,
                                 command=lambda: self._toggle_pin(r["key"]))
        if managed and r["summary"] is not None:
            versions = sorted({v for s in self._index if s.id == r["key"] for v in s.versions},
                              key=_parse_version, reverse=True)
            if versions:
                menu.add_separator()
                menu.add_command(label="Change Version", command=lambda: ModVersionWindow(
                    self, r["summary"], versions, r["installed"], list_repos(load_cfg()),
                    on_install=lambda v: self._install(r["summary"], v)))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _on_uninstall(self):
        if self._busy:
            return
        r = self._selected_row()
        if not r or r["kind"] != "managed":
            return
        others = [t for t in r["targets"] if t != self._target]
        also = (f" It's also installed for {target_label(others).replace(' only', '')} "
                f"in this game folder, which goes too." if others else "")
        if not messagebox.askyesno("Remove mod",
                f"Remove {r['name']} from your game?\n\nThis deletes its folder from "
                f"Mods/.{also} Libraries shared with other installed mods are left "
                f"in place.", parent=self):
            return
        # After the confirm, which only blocks this window: the game may have
        # been launched while it sat open.
        if not confirm_while_game_running(self, self._is_game_running,
                                          f"Removing {r['name']}"):
            return
        self._set_busy(True, f"Removing {r['name']}...")
        game_dir, name, mod_id = self._game_dir, r["name"], r["key"]
        run_bg(self, lambda: uninstall_mod(game_dir, mod_id),
               on_ok=lambda removed: self._finish(
                   f"{name} removed." if removed else f"{name} was not installed."),
               on_err=lambda e: self._finish(f"Remove failed: {e}"))

    def _on_toggle(self):
        if self._busy:
            return
        r = self._selected_row()
        if not r:
            return
        disabling = r["enabled"] is True
        # A disable is a rename that survives a running game; an enable may
        # write libraries back into UserLibs/.
        if not disabling and not confirm_while_game_running(
                self, self._is_game_running, f"Enabling {r['name']}"):
            return
        self._set_busy(True, f"{'Disabling' if disabling else 'Enabling'} {r['name']}...")
        game_dir, name, kind, key = self._game_dir, r["name"], r["kind"], r["key"]
        progress = lambda m: post(self, self._status.set, m)
        def toggle():
            if kind in ("managed", "untracked_folder"):
                ok = disable_mod(game_dir, key) if disabling else enable_mod(game_dir, key)
                if ok and not disabling and kind == "managed":
                    # The join prunes libraries no enabled mod pins.
                    restored = ensure_mod_libraries(game_dir, key, progress)
                    if restored:
                        progress(f"Restored {', '.join(restored)} for {name}.")
            else:
                ok = (disable_untracked_dll(game_dir, key) if disabling
                      else enable_untracked_dll(game_dir, key))
            verb = "disabled" if disabling else "enabled"
            return f"{name} {verb}." if ok else f"Couldn't {verb[:-1]} {name}."
        run_bg(self, toggle, on_ok=self._finish,
               on_err=lambda e: self._finish(f"Toggle failed: {e}"))

    def _toggle_pin(self, mod_id):
        cfg = load_cfg()
        try:
            if mod_id in self._pinned_ids:
                remove_pin(cfg, mod_id)
            else:
                add_pin(cfg, mod_id)
        except ModManagerError as e:
            self._status.set(str(e))
            return
        self._pinned_ids = {_parse_pin_entry(p)[0] for p in list_pinned(cfg)}

    def _open_community(self):
        if self._community_win and self._community_win.winfo_exists():
            self._community_win.lift(); return
        self._community_win = CommunityModsWindow(
            self, self._game_dir, self._target, self._community_changed,
            self._is_game_running, self._server_scene)

    def _community_changed(self):
        # Its installs dropped their own verify memo, so no forced reload.
        self._load(force=False)
        if self._on_change:
            self._on_change()

    def _on_export_modlist(self):
        if self._busy:
            return
        path = filedialog.asksaveasfilename(
            parent=self, title="Export Modlist", defaultextension=".json",
            filetypes=[("Modlist JSON", "*.json")])
        if not path:
            return
        pin = messagebox.askyesno("Pin exact versions?",
            "Pin every mod to its exact currently-installed version?\n\n"
            "Yes: a reproducible snapshot of this exact setup.\n"
            "No: each mod tracks whatever's latest when this file is later "
            "imported (a living modpack reference).", parent=self)
        try:
            modlist = export_modlist(self._game_dir, load_cfg(), self._target,
                                     pin_versions=pin)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(modlist, f, indent=2)
            note = (f" {len(modlist['untracked'])} untracked mod(s) are listed for "
                    f"reference but won't be installed by an import."
                    if modlist["untracked"] else "")
            self._status.set(
                f"Exported {len(modlist['mods'])} mod(s) to {os.path.basename(path)}.{note}")
        except Exception as e:
            messagebox.showerror("Export failed", str(e), parent=self)

    def _on_import_modlist(self):
        if self._busy:
            return
        path = filedialog.askopenfilename(parent=self, title="Import Modlist",
            filetypes=[("Modlist JSON", "*.json")])
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                modlist = json.load(f)
        except Exception as e:
            messagebox.showerror("Import failed", f"Couldn't read that file: {e}", parent=self)
            return

        self._set_busy(True, "Resolving modlist...")
        index, game_dir, target = self._index, self._game_dir, self._target
        def resolve():
            plan = import_modlist(modlist, index, list_repos(load_cfg()), target, game_dir)
            return plan, modlist_import_disables(game_dir, plan)
        run_bg(self, resolve, on_ok=lambda r: self._confirm_import(*r),
               on_err=lambda e: self._set_busy(False, f"Import failed: {e}"))

    def _confirm_import(self, plan, to_disable):
        self._set_busy(False)
        all_mods = plan.roots + plan.dependencies
        lines = []
        if all_mods:
            lines.append("Will install:")
            lines += [f"  • {m.id} {m.version}" for m in all_mods]
        if to_disable:
            lines.append("Will disable (not in this modlist):")
            lines += [f"  • {mid}" for mid in to_disable]
        if plan.unresolved:
            lines.append("Could not resolve (add the right source, then re-import):")
            lines += [f"  • {mid}" + (f" {v}" if v else "") for mid, v in plan.unresolved]
            if plan.hinted_repos:
                lines.append("This modlist expects sources: " + ", ".join(plan.hinted_repos))
        if plan.no_build:
            lines.append(f"Left out - no {target_label([plan.target]).replace(' only', '')} "
                         f"build, so they can't run here:")
            lines += [f"  • {mid} {v}" for mid, v in plan.no_build]
        if plan.untracked:
            lines.append("Not included - the machine this came from also ran these, "
                         "installed by hand. Copy them across yourself if you need them:")
            lines += [f"  • {u['name']}" for u in plan.untracked]
        if plan.blocking:
            messagebox.showerror("Modlist has unresolved mods", "\n".join(lines), parent=self)
            return
        if not all_mods and not to_disable:
            messagebox.showinfo("Nothing to import",
                "\n".join(lines + ["", "There's nothing new to install or disable."])
                if plan.untracked or plan.no_build else
                "This modlist has nothing new to install or disable.", parent=self)
            return
        if not messagebox.askyesno("Import modlist",
                "\n".join(lines) + "\n\nInstall these now?", parent=self):
            return
        if not confirm_while_game_running(self, self._is_game_running,
                                          "Importing a modlist"):
            return

        self._set_busy(True, "Installing...")
        game_dir = self._game_dir
        msg = f"Imported {len(plan.roots)} mod(s)."
        if to_disable:
            msg += f" Disabled {len(to_disable)}."
        run_bg(self, lambda: apply_import(game_dir, plan,
                                          lambda m: post(self, self._status.set, m)),
               on_ok=lambda _: self._finish(msg),
               on_err=lambda e: self._finish(f"Import failed: {e}"))


class ModVersionWindow(tk.Toplevel):
    """Every published version of one mod, with each one's requirements
    fetched lazily (the index doesn't carry them)."""
    _COLS   = ("version", "installed", "requires")
    _WIDTHS = (100,       80,          360)

    def __init__(self, parent, mod, versions, installed_version, repo_bases, on_install):
        super().__init__(parent)
        self.title(f"{mod.name} - Change Version")
        self.configure(bg=BG)
        self.geometry("580x380")
        self.minsize(480, 260)
        self._mod = mod
        self._versions = versions
        self._installed_version = installed_version
        self._repo_bases = repo_bases
        self._on_install = on_install
        self._closed = False
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        _enable_dark_titlebar(self)
        self.transient(parent)
        self._load_requires()

    def _build(self):
        _window_header(self, f"{self._mod.name} — Versions")

        tk.Label(self,
            text="Every release this mod has published, newest first. Mod authors "
                 "don't provide a release date, so none is shown here.",
            bg=BG, fg=MUTED, font=("Segoe UI",9), wraplength=540, justify="left"
        ).pack(anchor="w", padx=16, pady=(10,6))

        table_wrap = tk.Frame(self, bg=BG)
        table_wrap.pack(fill="both", expand=True, padx=16)
        self._tree = _mk_tree(table_wrap, self._COLS, self._WIDTHS, height=10)
        self._tree.tag_configure("installed", foreground=GREEN)
        self._tree.bind("<Double-1>", lambda e: self._on_install_click())
        for v in self._versions:
            mine = v == self._installed_version
            self._tree.insert("", "end", iid=v, values=(v, "Yes" if mine else "", "..."),
                              tags=("installed",) if mine else ())
        if self._versions:
            self._tree.selection_set(self._versions[0])

        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=16, pady=(8,4))
        _btn(bar, "Install Selected", self._on_install_click, style="primary",
             font=("Segoe UI",9), pady=5, padx=14).pack(side="left")

        self._status = tk.StringVar(value="Loading version details...")
        tk.Label(self, textvariable=self._status, bg=BG, fg=CYAN,
                 font=("Segoe UI",9), wraplength=540, justify="left"
        ).pack(anchor="w", padx=16, pady=(0,10))

    def _load_requires(self):
        mod, repo_bases, versions = self._mod, self._repo_bases, self._versions
        def fetch():
            for v in versions:
                if self._closed:
                    return
                try:
                    deps = _fetch_manifest(repo_bases, mod.id, v, prefer_repo=mod.source_repo).dependencies
                    text = ", ".join(f"{dep_id}>={min_v}" for dep_id, min_v in sorted(deps.items())) or "-"
                except ModManagerError:
                    text = "?"
                post(self, self._apply_requires, v, text)
        run_bg(self, fetch, on_ok=lambda _: self._status.set(""))

    def _apply_requires(self, version, text):
        if self._tree.exists(version):
            values = list(self._tree.item(version, "values"))
            values[2] = text
            self._tree.item(version, values=values)

    def _on_install_click(self):
        sel = self._tree.selection()
        if not sel:
            return
        self.destroy()
        self._on_install(sel[0])

    def destroy(self):
        self._closed = True   # stops the manifest fetches early
        super().destroy()
