"""Browse the merged repo index and install something new."""
import threading
import tkinter as tk
from tkinter import messagebox

from tavern_shared.mod_install import (
    _melonloader_installed, _tavernlib_installed,
)
from tavern_shared.theme import (
    AMBER, BG, BORDER, CYAN, GREEN, MUTED, PARCH, RED, SURF, _btn, _mk_scrollbar,
)
from tavern_shared.window_chrome import _enable_dark_titlebar

from tavern_shared.mods.cache import clear_mod_cache
from tavern_shared.mods.errors import ModManagerError
from tavern_shared.mods.hostcfg import load_cfg
from tavern_shared.mods.install import (
    install_mod_closure, list_installed_mods, list_mods, mod_status,
)
from tavern_shared.mods.repos import (
    _is_default, _source_display, fetch_indexes, list_repos,
)
from tavern_shared.mods.resolve import resolve_mod_by_id
from tavern_shared.mods.ui.sources_window import ModSourcesWindow


class CommunityModsWindow(tk.Toplevel):
    """Card-based browser for community mods pulled from the configured sources.
    Lists every available mod - description, dependencies, client/server/parity
    info and all - with a per-card install/update/reinstall button, plus a
    search box over name/author/id/description. A way of ADDING mods. Managing
    what's already installed (uninstall, enable/disable, keep-enabled, change
    version, import/export) lives one level up in ModManagerWindow, which this
    window is normally opened from. All network and disk work runs off the UI
    thread; the parent is refreshed through on_change after anything changes."""

    _COL_LABELS = ("Status", "Author", "Mod", "Installed", "Latest", "Source")
    _WIDTHS     = (90,       110,      160,   74,          64,       110)
    _STATE_WORD = {
        "missing":  "Available",
        "damaged":  "Damaged",
        "current":  "Installed",
        "outdated": "Update ready",
        "unknown":  "Installed",
    }
    _PRIMARY_LABEL = {
        "missing":  "Install",
        "damaged":  "Reinstall",
        "outdated": "Update",
        "current":  "Reinstall",
        "unknown":  "Reinstall",
    }
    _SIDE_LABEL = {
        (True, True):   "Client + Server",
        (True, False):  "Client Only",
        (False, True):  "Server Only",
        (False, False): "-",
    }

    def __init__(self, parent, game_dir, side="client", on_change=None):
        super().__init__(parent)
        self.title("Community Mods")
        self.configure(bg=BG)
        self.geometry("860x580")
        self.minsize(760, 440)
        self._game_dir  = game_dir
        self._side      = side
        self._on_change = on_change
        self._index     = []      # list[ModSummary], the merged raw index
        self._rows      = {}      # id -> row dict (see _rebuild_rows)
        self._visible_ids = []    # ids currently shown, filtered + sorted
        self._card_widgets = {}   # id -> widget refs for in-place updates
        # (id, version) -> resolved "Requires: ..." text, so re-searching or
        # reloading after an install doesn't re-fetch a manifest already seen
        # this session; keyed on version so a bumped release refetches.
        self._deps_cache   = {}
        self._deps_pending = set()
        self._busy      = False
        self._sources_win = None
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        _enable_dark_titlebar(self)
        self.transient(parent)
        self._load(force=False)

    def _build(self):
        # Resolved here rather than as a class attribute: PARCH/GREEN/AMBER/
        # MUTED are host colours wired in by set_helpers, which has always
        # run by the time a window's _build() executes, but not yet at class
        # -definition time (module import).
        self._state_color = {
            "missing": PARCH, "damaged": RED, "current": GREEN,
            "outdated": AMBER, "unknown": MUTED,
        }
        h = tk.Frame(self, bg=SURF, height=44)
        h.pack(fill="x"); h.pack_propagate(False)
        tk.Label(h, text="Community Mods", bg=SURF, fg=AMBER,
                 font=("Georgia",12,"bold")).pack(side="left", padx=16, pady=8)
        _btn(h, "Manage Sources", self._open_sources, style="dim",
             font=("Segoe UI",9), pady=4, padx=10).pack(side="right", padx=12)
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

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
        self.v_search.trace_add("write", lambda *_: self._apply_filter())
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

        # A Treeview can't host the description/dependency lines each card
        # needs below its row of columns, so the list itself is a scrollable
        # stack of card frames instead (same approach as ModDiffWindow).
        canvas_frame = tk.Frame(list_wrap, bg=BG)
        canvas_frame.pack(fill="both", expand=True)
        canvas = tk.Canvas(canvas_frame, bg=BG, highlightthickness=0)
        vsb = _mk_scrollbar(canvas_frame, canvas.yview)
        vsb.pack(side="right", fill="y")
        canvas.config(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        self._canvas = canvas
        self._cards_frame = tk.Frame(canvas, bg=BG)
        window = canvas.create_window((0, 0), window=self._cards_frame, anchor="nw")
        self._cards_frame.bind("<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(window, width=e.width))
        def _mousewheel(event):
            if self._cards_frame.winfo_height() <= canvas.winfo_height():
                return
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind("<MouseWheel>", _mousewheel)
        self._cards_frame.bind("<MouseWheel>", _mousewheel)

        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=16, pady=(8,4))
        self._refresh_btn = _btn(bar, "Refresh", lambda: self._load(force=True),
                                 style="dim", font=("Segoe UI",9), pady=5, padx=12)
        self._refresh_btn.pack(side="right")
        if self._side == "client":
            self._clear_cache_btn = _btn(bar, "Clear Mod Cache", self._on_clear_cache,
                                         style="dim", font=("Segoe UI",9), pady=5, padx=12)
            self._clear_cache_btn.pack(side="right", padx=(0,8))

        self._status = tk.StringVar(value="Loading community mods...")
        tk.Label(self, textvariable=self._status, bg=BG, fg=CYAN,
                 font=("Segoe UI",9), wraplength=800, justify="left"
        ).pack(anchor="w", padx=16, pady=(10,10))

    # -- data loading --

    def _load(self, force=False):
        self._set_busy(True, "Loading community mods...")
        def worker():
            try:
                repos = list_repos(load_cfg())
                index = fetch_indexes(repos, force=force)
                available = list_mods(index, self._side)
                installed = list_installed_mods(self._game_dir)
                self.after(0, lambda: self._render(index, available, installed))
            except Exception as e:
                self.after(0, lambda e=e: self._load_failed(e))
        threading.Thread(target=worker, daemon=True).start()

    def _load_failed(self, err):
        self._set_busy(False, f"Couldn't load community mods: {err}")

    def _render(self, index, available, installed):
        self._index = index
        self._rebuild_rows(available, installed)
        self._populate_cards()
        self._set_busy(False, "")
        self._refresh_states()

    def _rebuild_rows(self, available, installed):
        # installed is cross-referenced only for the "Installed" column's
        # display value - actually managing that install (uninstall,
        # enable/disable, version) is ModManagerWindow's job, not this
        # window's, so a mod that's dropped out of every index no longer
        # gets a row here at all (it still has one there).
        installed_by_id = {m["id"]: m for m in installed
                           if m.get("id") and self._meta_matches_side(m)}
        rows = {}
        for s in available:
            meta = installed_by_id.get(s.id)
            rows[s.id] = {
                "id": s.id, "name": s.name, "author": s.author or "",
                "description": s.description or "",
                "client_side": s.client_side, "server_side": s.server_side,
                "parity_required": s.parity_required,
                "latest": s.highest(), "source": self._source_label(s.source_repo),
                "summary": s, "installed": meta.get("version", "?") if meta else None,
                "state": "missing",
            }
        self._rows = rows

    def _meta_matches_side(self, meta):
        key = "client_side" if self._side == "client" else "server_side"
        return meta.get(key, True)

    def _source_label(self, source_repo):
        if _is_default(source_repo):
            return "Modding Tavern"
        return _source_display(source_repo)

    # -- card rendering --

    def _populate_cards(self):
        """Rebuilds every card from scratch - called when the underlying data
        actually changes (a fresh load, or after an install/uninstall), never
        on every keystroke. Typing in the search box only re-shows/hides
        these same card widgets (see _apply_filter); destroying and
        recreating them per keystroke was what made the buttons flicker."""
        for child in self._cards_frame.winfo_children():
            child.destroy()
        self._card_widgets = {}
        for mod_id in sorted(self._rows, key=lambda i: self._rows[i]["name"].lower()):
            self._mk_card(mod_id)
        self._apply_filter()

    def _apply_filter(self):
        """Shows/hides the already-built cards to match the search box,
        without touching any widget's identity - pack_forget/pack only, so
        nothing flickers. Order is re-asserted on every call (forgetting
        every card, then re-packing only the visible ones in name order)
        since a widget that's re-packed after being forgotten would
        otherwise land at the end of the stack instead of back in its
        alphabetical slot."""
        query = self.v_search.get().strip().lower()
        def matches(mod_id):
            if not query:
                return True
            r = self._rows[mod_id]
            hay = f"{r['name']} {r['author']} {r['id']} {r['description']}".lower()
            return query in hay
        all_ids = sorted(self._rows, key=lambda i: self._rows[i]["name"].lower())
        self._visible_ids = [mid for mid in all_ids if matches(mid)]

        for mod_id in all_ids:
            self._card_widgets[mod_id]["card"].pack_forget()
        for mod_id in self._visible_ids:
            self._card_widgets[mod_id]["card"].pack(fill="x", pady=(0,6))
        self._cards_frame.update_idletasks()
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

        total, shown = len(self._rows), len(self._visible_ids)
        if query:
            self._status.set(f"{shown} of {total} mod(s) match '{query}'."
                             if total else self._status.get())
        self._queue_dep_fetches()

    def _mk_card(self, mod_id):
        # Not packed here - _apply_filter (called right after every card is
        # built) is solely responsible for packing/unpacking cards, so there
        # aren't two code paths fighting over each card's geometry.
        r = self._rows[mod_id]
        card = tk.Frame(self._cards_frame, bg=SURF,
                        highlightbackground=BORDER, highlightthickness=1)

        top = tk.Frame(card, bg=SURF)
        top.pack(fill="x", pady=(8,0))
        cells = []
        for width in self._WIDTHS:
            cell = tk.Frame(top, bg=SURF, width=width, height=26)
            cell.pack(side="left", fill="y"); cell.pack_propagate(False)
            cells.append(cell)
        status_cell, author_cell, mod_cell, installed_cell, latest_cell, source_cell = cells

        status_var = tk.StringVar(value=self._STATE_WORD.get(r["state"], ""))
        status_lbl = tk.Label(status_cell, textvariable=status_var, bg=SURF,
                              font=("Segoe UI",9), anchor="w")
        status_lbl.pack(fill="both", padx=6)
        tk.Label(author_cell, text=r["author"] or "-", bg=SURF, fg=PARCH,
                 font=("Segoe UI",9), anchor="w").pack(fill="both", padx=6)
        tk.Label(mod_cell, text=r["name"], bg=SURF, fg=PARCH,
                 font=("Segoe UI",9,"bold"), anchor="w").pack(fill="both", padx=6)
        tk.Label(installed_cell, text=r["installed"] or "-", bg=SURF, fg=PARCH,
                 font=("Segoe UI",9), anchor="w").pack(fill="both", padx=6)
        tk.Label(latest_cell, text=r["latest"], bg=SURF, fg=PARCH,
                 font=("Segoe UI",9), anchor="w").pack(fill="both", padx=6)
        tk.Label(source_cell, text=r["source"], bg=SURF, fg=PARCH,
                 font=("Segoe UI",9), anchor="w").pack(fill="both", padx=6)

        btn = _btn(top, self._PRIMARY_LABEL.get(r["state"], "Install"),
                  lambda mid=mod_id: self._on_install_click(mid),
                  style="primary", font=("Segoe UI",9), pady=4, padx=10, width=11)
        btn.pack(side="right", padx=(6,10))

        side_text = self._SIDE_LABEL[(r["client_side"], r["server_side"])]
        parity_text = "Exact version required" if r["parity_required"] else "Any compatible version"
        tk.Label(card, text=f"{side_text}  ·  {parity_text}", bg=SURF, fg=CYAN,
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
            "card": card, "status_var": status_var, "status_lbl": status_lbl,
            "btn": btn, "requires_var": requires_var,
        }
        self._apply_card_state(mod_id)

    def _apply_card_state(self, mod_id):
        r = self._rows.get(mod_id)
        w = self._card_widgets.get(mod_id)
        if not r or not w:
            return
        state = r["state"]
        w["status_var"].set(self._STATE_WORD.get(state, ""))
        w["status_lbl"].config(fg=self._state_color.get(state, MUTED))
        w["btn"].config(text=self._PRIMARY_LABEL.get(state, "Install"),
                        state="disabled" if (self._busy or r["summary"] is None) else "normal")

    def _refresh_states(self):
        if not self._rows:
            return
        ids, index, game_dir = list(self._rows), self._index, self._game_dir
        def worker():
            states = {}
            for mod_id in ids:
                try:
                    states[mod_id] = mod_status(game_dir, mod_id, index)
                except Exception:
                    states[mod_id] = "unknown"
            self.after(0, lambda: self._apply_states(states))
        threading.Thread(target=worker, daemon=True).start()

    def _apply_states(self, states):
        for mod_id, st in states.items():
            if mod_id in self._rows:
                self._rows[mod_id]["state"] = st
        # All cards, not just the currently-visible ones, so a card hidden
        # by the search filter is still up to date whenever it reappears.
        for mod_id in self._card_widgets:
            self._apply_card_state(mod_id)

    # -- dependencies (async, cached per id+version) --

    def _queue_dep_fetches(self):
        repos = list_repos(load_cfg())
        to_fetch = []
        for mod_id in self._visible_ids:
            r = self._rows[mod_id]
            if r["summary"] is None:
                continue
            key = (mod_id, r["latest"])
            if key in self._deps_cache:
                self._apply_deps_text(mod_id, self._deps_cache[key])
                continue
            if key in self._deps_pending:
                continue
            self._deps_pending.add(key)
            to_fetch.append((mod_id, key, r["summary"]))
        if not to_fetch:
            return
        def worker():
            for mod_id, key, summary in to_fetch:
                try:
                    manifest = resolve_mod_by_id(repos, mod_id, prefer_repo=summary.source_repo)
                    deps = manifest.dependencies or {}
                    text = ("Requires: " + ", ".join(f"{d}>={v}" for d, v in sorted(deps.items()))
                           if deps else "No dependencies.")
                except ModManagerError:
                    text = "Couldn't load dependency info."
                self._deps_cache[key] = text
                self._deps_pending.discard(key)
                self.after(0, lambda mid=mod_id, t=text: self._apply_deps_text(mid, t))
        threading.Thread(target=worker, daemon=True).start()

    def _apply_deps_text(self, mod_id, text):
        w = self._card_widgets.get(mod_id)
        if w:
            w["requires_var"].set(text)

    # -- actions --

    def _on_install_click(self, mod_id):
        if self._busy:
            return
        r = self._rows.get(mod_id)
        if not r or r["summary"] is None:
            return
        self._install(r["summary"])

    def _install(self, mod, version=None):
        """Installs `mod`'s closure, at `version` if given, else
        mod.highest() (the ordinary Install/Update/Reinstall path)."""
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
        label = f"{mod.name} {version}" if version else mod.name
        self._set_busy(True, f"Installing {label}...")
        index = self._index
        def worker():
            try:
                repos = list_repos(load_cfg())
                install_mod_closure(
                    self._game_dir, mod, index, repos,
                    lambda m: self.after(0, lambda: self._status.set(m)),
                    self._side, version=version)
                self.after(0, lambda: self._finish(f"{label} installed."))
            except Exception as e:
                self.after(0, lambda e=e: self._finish(f"Install failed: {e}"))
        threading.Thread(target=worker, daemon=True).start()

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
        def worker():
            try:
                clear_mod_cache()
                self.after(0, lambda: self._finish("Mod cache cleared."))
            except Exception as e:
                self.after(0, lambda e=e: self._finish(f"Clear cache failed: {e}"))
        threading.Thread(target=worker, daemon=True).start()

    def _finish(self, msg):
        self._set_busy(False, msg)
        # The available set didn't change, only what's on disk, so re-scan
        # installed state locally rather than re-fetching the index.
        self._reconcile_installed()
        self._refresh_states()
        if self._on_change:
            self._on_change()

    def _reconcile_installed(self):
        try:
            available = list_mods(self._index, self._side)
            installed = list_installed_mods(self._game_dir)
        except Exception:
            return
        self._rebuild_rows(available, installed)
        self._populate_cards()

    def _open_sources(self):
        if self._busy:
            return
        if self._sources_win and self._sources_win.winfo_exists():
            self._sources_win.lift(); return
        self._sources_win = ModSourcesWindow(self, on_change=lambda: self._load(force=True))

    def _set_busy(self, busy, msg=""):
        self._busy = busy
        self._refresh_btn.config(state="disabled" if busy else "normal")
        if msg or not busy:
            self._status.set(msg)
        for mod_id in self._card_widgets:
            self._apply_card_state(mod_id)
