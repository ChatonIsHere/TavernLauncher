"""Pick a specific version of an already-installed mod."""
import threading
import tkinter as tk

from tavern_shared.theme import (
    AMBER, BG, BORDER, CYAN, GREEN, MUTED, SURF, _btn, _mk_tree,
)
from tavern_shared.window_chrome import _enable_dark_titlebar

from tavern_shared.mods.errors import ModManagerError
from tavern_shared.mods.resolve import _fetch_manifest


class ModVersionWindow(tk.Toplevel):
    """Every version one mod has published, so a user can install something
    other than mod.highest(). No release-date column: neither manifest.json
    nor repository.json carries a date anywhere in the schema (see the module
    docstring's field lists), so there's genuinely nothing to show there -
    fabricating one would be worse than omitting it. "Requires" is the one
    piece of real per-version metadata available (dependencies can change
    release to release, unlike author/description, which don't), fetched
    lazily per row since getting it means pulling that version's full
    manifest, not just the slim index entry."""
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
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        _enable_dark_titlebar(self)
        self.transient(parent)
        self._load_requires()

    def _build(self):
        h = tk.Frame(self, bg=SURF, height=44)
        h.pack(fill="x"); h.pack_propagate(False)
        tk.Label(h, text=f"{self._mod.name} — Versions", bg=SURF, fg=AMBER,
                 font=("Georgia",12,"bold")).pack(side="left", padx=16, pady=8)
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

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
            tag = ("installed",) if v == self._installed_version else ()
            label = "Yes" if v == self._installed_version else ""
            self._tree.insert("", "end", iid=v, values=(v, label, "..."), tags=tag)
        if self._versions:
            self._tree.selection_set(self._versions[0])

        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=16, pady=(8,4))
        self._install_btn = _btn(bar, "Install Selected", self._on_install_click,
                                 style="primary", font=("Segoe UI",9), pady=5, padx=14)
        self._install_btn.pack(side="left")

        self._status = tk.StringVar(value="Loading version details...")
        tk.Label(self, textvariable=self._status, bg=BG, fg=CYAN,
                 font=("Segoe UI",9), wraplength=540, justify="left"
        ).pack(anchor="w", padx=16, pady=(0,10))

    def _load_requires(self):
        mod, repo_bases, versions = self._mod, self._repo_bases, self._versions
        def worker():
            for v in versions:
                try:
                    manifest = _fetch_manifest(repo_bases, mod.id, v, prefer_repo=mod.source_repo)
                    deps = manifest.dependencies or {}
                    text = ", ".join(f"{dep_id}>={min_v}" for dep_id, min_v in sorted(deps.items())) or "-"
                except ModManagerError:
                    text = "?"
                self.after(0, lambda v=v, t=text: self._apply_requires(v, t))
            self.after(0, lambda: self._status.set(""))
        threading.Thread(target=worker, daemon=True).start()

    def _apply_requires(self, version, text):
        if self._tree.exists(version):
            values = list(self._tree.item(version, "values"))
            values[2] = text
            self._tree.item(version, values=values)

    def _on_install_click(self):
        sel = self._tree.selection()
        if not sel:
            return
        version = sel[0]
        self.destroy()
        self._on_install(version)
