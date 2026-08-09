"""Manage Sources: the user's community repositories."""
import threading
import tkinter as tk
from tkinter import messagebox
from tkinter import simpledialog

from tavern_shared.theme import (
    AMBER, AMBERDIM, BG, BORDER, CYAN, MUTED, PARCH, SURF, _btn,
)
from tavern_shared.window_chrome import _enable_dark_titlebar

from tavern_shared.mods.hostcfg import load_cfg
from tavern_shared.mods.repos import (
    _is_default, _source_display, add_repo, list_repos, remove_repo,
)


class ModSourcesWindow(tk.Toplevel):
    """Thin manager for the user's community repositories: a listbox plus
    add/remove, calling straight into modmanager.list_repos/add_repo/remove_repo.
    The default Modding Tavern repo is shown but can't be removed. Adding a repo
    shows the unverified-source warning first."""
    def __init__(self, parent, on_change=None):
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
        h = tk.Frame(self, bg=SURF, height=44)
        h.pack(fill="x"); h.pack_propagate(False)
        tk.Label(h, text="Mod Sources", bg=SURF, fg=AMBER,
                 font=("Georgia",12,"bold")).pack(side="left", padx=16, pady=8)
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

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
        def worker():
            try:
                cfg = load_cfg()
                add_repo(cfg, url)
                self.after(0, lambda: self._added_ok())
            except Exception as e:
                self.after(0, lambda e=e: self._status.set(f"Couldn't add source: {e}"))
        threading.Thread(target=worker, daemon=True).start()

    def _added_ok(self):
        self._status.set("Source added.")
        self._reload()
        if self._on_change:
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
        self._status.set("Source removed.")
        self._reload()
        if self._on_change:
            self._on_change()
