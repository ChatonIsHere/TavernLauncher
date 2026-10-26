"""Browse the merged repo index and install something new."""
import tkinter as tk
from tkinter import messagebox

from tavern_shared.bg import post, run_bg
from tavern_shared.theme import (
    AMBER, BG, BORDER, CYAN, GREEN, MUTED, PARCH, RED, SURF, _btn,
    _scroll_stack, _window_header,
)
from tavern_shared.window_chrome import _enable_dark_titlebar

from tavern_shared.mods.cache import clear_mod_cache
from tavern_shared.mods.base import ModManagerError
from tavern_shared.mods.base import load_cfg
from tavern_shared.mods.install import list_installed_mods, list_mods
from tavern_shared.mods.repos import fetch_indexes, list_repos
from tavern_shared.mods.resolve import resolve_mod_by_id
from tavern_shared.mods.ui.shared import (
    ModListWindow, install_mod_flow, record_matches_target, source_label,
    target_label,
)
from tavern_shared.mods.ui.sources_window import ModSourcesWindow


# (id, version) -> "Requires: ..." text. Process-wide, so reopening the window
# or reloading after an install doesn't refetch a manifest already seen;
# keyed on version so a new release refetches.
_DEPS_CACHE = {}


class CommunityModsWindow(ModListWindow):
    """Card-based browser for community mods pulled from the configured sources.
    Lists every mod with a build for this launcher's target - description,
    dependencies, targets/parity info and all - with a per-card install/update/reinstall button, plus a
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
    _STATE_COLOR = {
        "missing": PARCH, "damaged": RED, "current": GREEN,
        "outdated": AMBER, "unknown": MUTED,
    }
    _PRIMARY_LABEL = {
        "missing":  "Install",
        "damaged":  "Reinstall",
        "outdated": "Update",
        "current":  "Reinstall",
        "unknown":  "Reinstall",
    }
    def __init__(self, parent, game_dir, target="pc", on_change=None,
                 is_game_running=None, server_scene=None):
        super().__init__(parent)
        self.title("Community Mods")
        self.configure(bg=BG)
        self.geometry("860x580")
        self.minsize(760, 440)
        self._game_dir  = game_dir
        self._target    = target
        self._on_change = on_change
        # Handed down from the Mod Manager, so an install from here asks the
        # same questions as one from there.
        self._is_game_running = is_game_running
        self._server_scene = server_scene
        self._index     = []      # list[ModSummary], the merged raw index
        self._repos     = []      # the repo bases _index came from
        self._rows      = {}      # id -> row dict (see _rebuild_rows)
        self._visible_ids = []    # ids currently shown, filtered + sorted
        self._card_widgets = {}   # id -> widget refs for in-place updates
        self._deps_pending = set()
        self._filter_job = None
        self._busy      = False
        self._sources_win = None
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        _enable_dark_titlebar(self)
        self.transient(parent)
        self._load(force=False)

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
        # Cards rather than a Treeview: each needs description and dependency
        # lines under its row of columns.
        self._canvas, self._cards_frame = _scroll_stack(list_wrap)

        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=16, pady=(8,4))
        self._refresh_btn = _btn(bar, "Refresh", lambda: self._load(force=True),
                                 style="dim", font=("Segoe UI",9), pady=5, padx=12)
        self._refresh_btn.pack(side="right")
        if self._target == "pc":
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
        game_dir, target = self._game_dir, self._target
        def load():
            repos = list_repos(load_cfg())
            index = fetch_indexes(repos, force=force)
            return repos, index, list_mods(index, target), list_installed_mods(game_dir)
        run_bg(self, load, on_ok=lambda r: self._render(*r),
               on_err=lambda e: self._set_busy(False, f"Couldn't load community mods: {e}"))

    def _render(self, repos, index, available, installed):
        self._repos, self._index = repos, index
        self._rebuild_rows(available, installed)
        self._populate_cards()
        self._set_busy(False, "")
        self._refresh_states()

    def _rebuild_rows(self, available, installed):
        # installed only feeds the "Installed" column; managing an install is
        # ModManagerWindow's job, so a mod no index lists has no row here.
        installed_by_id = self._installed_versions(installed)
        rows = {}
        for s in available:
            rows[s.id] = {
                "id": s.id, "name": s.name, "author": s.author or "",
                "description": s.description or "",
                "targets": s.targets,
                "parity_required": s.parity_required,
                "scenes": s.scenes,
                "latest": s.highest(), "source": source_label(s.source_repo),
                "summary": s, "installed": installed_by_id.get(s.id),
                "state": "missing",
            }
        self._rows = rows

    def _installed_versions(self, installed):
        return {m["id"]: m.get("version", "?") for m in installed
                if m.get("id") and record_matches_target(m, self._target)}

    # -- card rendering --

    def _populate_cards(self):
        """Builds every card from scratch, on a fresh load only. Searching
        re-packs these same widgets and an install updates them in place."""
        for child in self._cards_frame.winfo_children():
            child.destroy()
        self._card_widgets = {}
        self._visible_ids = []
        for mod_id in self._sorted_ids():
            self._mk_card(mod_id)
        self._apply_filter()

    def _sorted_ids(self):
        return sorted(self._rows, key=lambda i: self._rows[i]["name"].lower())

    def _schedule_filter(self):
        if self._filter_job:
            self.after_cancel(self._filter_job)
        self._filter_job = self.after(150, self._apply_filter)

    def _apply_filter(self):
        """Shows the cards matching the search box, in name order. Only
        re-packs when the visible set changes; a card re-packed after being
        forgotten lands at the end, so all of them go and come back."""
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

        total = len(self._rows)
        if query and total:
            self._status.set(f"{len(visible)} of {total} mod(s) match '{query}'.")
        self._queue_dep_fetches()

    def _mk_card(self, mod_id):
        # Not packed here: _apply_filter alone owns packing.
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
        installed_var = tk.StringVar(value=r["installed"] or "-")
        tk.Label(installed_cell, textvariable=installed_var, bg=SURF, fg=PARCH,
                 font=("Segoe UI",9), anchor="w").pack(fill="both", padx=6)
        tk.Label(latest_cell, text=r["latest"], bg=SURF, fg=PARCH,
                 font=("Segoe UI",9), anchor="w").pack(fill="both", padx=6)
        tk.Label(source_cell, text=r["source"], bg=SURF, fg=PARCH,
                 font=("Segoe UI",9), anchor="w").pack(fill="both", padx=6)

        btn = _btn(top, self._PRIMARY_LABEL.get(r["state"], "Install"),
                  lambda mid=mod_id: self._on_install_click(mid),
                  style="primary", font=("Segoe UI",9), pady=4, padx=10, width=11)
        btn.pack(side="right", padx=(6,10))

        parity_text = "Exact version required" if r["parity_required"] else "Any compatible version"
        tag_text = f"{target_label(r['targets'])}  ·  {parity_text}"
        if r.get("scenes"):
            # Shown only when a mod narrows it down; no segment means every scene.
            tag_text += "  ·  " + ", ".join(s.capitalize() for s in r["scenes"]) + " scene" \
                + ("s" if len(r["scenes"]) > 1 else "") + " only"
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
            "card": card, "status_var": status_var, "status_lbl": status_lbl,
            "installed_var": installed_var, "btn": btn, "requires_var": requires_var,
        }
        self._apply_card_state(mod_id)

    def _apply_card_state(self, mod_id):
        r = self._rows.get(mod_id)
        w = self._card_widgets.get(mod_id)
        if not r or not w:
            return
        state = r["state"]
        w["status_var"].set(self._STATE_WORD.get(state, ""))
        w["status_lbl"].config(fg=self._STATE_COLOR.get(state, MUTED))
        w["btn"].config(text=self._PRIMARY_LABEL.get(state, "Install"),
                        state="disabled" if (self._busy or r["summary"] is None) else "normal")

    # -- ModListWindow hooks --

    def _busy_changed(self):
        for mod_id in self._card_widgets:
            self._apply_card_state(mod_id)

    def _state_ids(self):
        return list(self._rows)

    def _apply_states(self, states):
        for mod_id, st in states.items():
            if mod_id in self._rows:
                self._rows[mod_id]["state"] = st
        # Hidden cards too, so one is current whenever the filter shows it.
        self._busy_changed()

    def _read_installed(self):
        return list_installed_mods(self._game_dir)

    def _apply_installed(self, installed):
        versions = self._installed_versions(installed)
        for mod_id, r in self._rows.items():
            r["installed"] = versions.get(mod_id)
            w = self._card_widgets.get(mod_id)
            if w:
                w["installed_var"].set(r["installed"] or "-")

    # -- dependencies (async, cached per id+version) --

    def _queue_dep_fetches(self):
        to_fetch = []
        for mod_id in self._visible_ids:
            r = self._rows[mod_id]
            if r["summary"] is None:
                continue
            key = (mod_id, r["latest"])
            if key in _DEPS_CACHE:
                self._apply_deps_text(mod_id, _DEPS_CACHE[key])
            elif key not in self._deps_pending:
                self._deps_pending.add(key)
                to_fetch.append((mod_id, key, r["summary"]))
        if not to_fetch:
            return
        repos = self._repos
        def fetch():
            for mod_id, key, summary in to_fetch:
                try:
                    manifest = resolve_mod_by_id(repos, mod_id, prefer_repo=summary.source_repo)
                    deps = manifest.dependencies or {}
                    text = ("Requires: " + ", ".join(f"{d}>={v}" for d, v in sorted(deps.items()))
                           if deps else "No dependencies.")
                except ModManagerError:
                    text = "Couldn't load dependency info."
                _DEPS_CACHE[key] = text
                post(self, self._deps_fetched, key, mod_id, text)
        run_bg(self, fetch)

    def _deps_fetched(self, key, mod_id, text):
        self._deps_pending.discard(key)
        self._apply_deps_text(mod_id, text)

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
        install_mod_flow(self, r["summary"])

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
