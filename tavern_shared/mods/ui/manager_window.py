"""The Mod Manager: everything currently sitting in Mods/ on this machine."""
import json
import os
import threading
import tkinter as tk
from tkinter import filedialog
from tkinter import messagebox

from tavern_shared.theme import (
    AMBER, AMBERDIM, BG, BORDER, CYAN, GREEN, MUTED, PARCH, RED, SURF, _btn,
    _mk_tree,
)
from tavern_shared.game_guard import confirm_while_game_running
from tavern_shared.window_chrome import _enable_dark_titlebar

from tavern_shared.mods.cache import ensure_mod_libraries
from tavern_shared.mods.errors import ModManagerError
from tavern_shared.mods.hostcfg import load_cfg
from tavern_shared.mods.install import (
    _invalidate_verify_memo, disable_mod, disable_untracked_dll, enable_mod,
    enable_untracked_dll, list_installed_mods, list_untracked_mods,
    mod_status, uninstall_mod,
)
from tavern_shared.mods.modlist import (
    apply_import, export_modlist, import_modlist, modlist_import_disables,
)
from tavern_shared.mods.pins import (
    _parse_pin_entry, add_pin, list_pinned, remove_pin,
)
from tavern_shared.mods.repos import fetch_indexes, list_repos
from tavern_shared.mods.ui.community_window import CommunityModsWindow
from tavern_shared.mods.ui.shared import (
    install_mod_flow, meta_matches_side, source_label,
)
from tavern_shared.mods.ui.version_window import ModVersionWindow
from tavern_shared.mods.version import _parse_version


class ModManagerWindow(tk.Toplevel):
    """Everything currently sitting in Mods/ on this machine: mods this
    manager installed (folder + manifest.json/manifest.disabled.json - see
    list_installed_mods) and mods that got there some other way (a loose
    root .dll, or a folder with a manifest this manager didn't write - see
    list_untracked_mods), told apart by the "Managed"/"Untracked" column.
    This is where an already-installed mod actually gets MANAGED - enable/
    disable, uninstall, keep-enabled, change version, import/export a
    modlist. Browsing the full repo to add something new is the separate,
    narrower CommunityModsWindow, opened from here rather than shown inline,
    so this table only ever lists what's really on disk."""

    _COLS   = ("status", "author", "mod", "installed", "latest", "managed", "source")
    _WIDTHS = (90,       110,      160,   74,          70,       80,        120)
    _STATE_WORD = {
        "missing":  "Missing",
        "damaged":  "Damaged",
        "current":  "Up to date",
        "outdated": "Update ready",
        "unknown":  "Installed",
    }

    def __init__(self, parent, game_dir, side="client", on_change=None,
                 is_game_running=None):
        super().__init__(parent)
        self.title("Mod Manager")
        self.configure(bg=BG)
        self.geometry("800x480")
        self.minsize(680, 380)
        self._game_dir  = game_dir
        self._side      = side
        self._on_change = on_change
        # Optional "is the game up?" check from the owning launcher (see
        # tavern_shared.game_guard). The window itself opens either way -
        # reading the table is harmless - and only the actions that write into
        # Mods/ ask. A launcher that passes nothing behaves as before.
        self._is_game_running = is_game_running
        self._index     = []      # list[ModSummary], the merged raw index
        self._rows      = {}      # row id -> row dict (see _rebuild_rows)
        self._busy      = False
        self._community_win = None
        # Pinning is a join-time concept - only the client renders a
        # per-server active set at all; a launcher-run server just has one
        # fixed Mods/, with nothing to pin against.
        self._pinned_ids = set()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        _enable_dark_titlebar(self)
        self.transient(parent)
        self._load(force=False)

    def _build(self):
        h = tk.Frame(self, bg=SURF, height=44)
        h.pack(fill="x"); h.pack_propagate(False)
        tk.Label(h, text="Mod Manager", bg=SURF, fg=AMBER,
                 font=("Georgia",12,"bold")).pack(side="left", padx=16, pady=8)
        _btn(h, "Community Mods", self._open_community, style="dim",
             font=("Segoe UI",9), pady=4, padx=10).pack(side="right", padx=12)
        # Modlist import/export applies to both sides - a client's Mods/ and
        # a launcher-run server's Mods/ are both real installed sets that can
        # be exported, and either can import one (the same file a headless
        # server's /modlist can point at directly, with zero conversion -
        # see export_modlist/import_modlist).
        _btn(h, "Import Modlist", self._on_import_modlist, style="dim",
             font=("Segoe UI",9), pady=4, padx=10).pack(side="right", padx=(12,0))
        _btn(h, "Export Modlist", self._on_export_modlist, style="dim",
             font=("Segoe UI",9), pady=4, padx=10).pack(side="right", padx=(12,0))
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

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
        self._tree.tag_configure("missing",   foreground=PARCH)
        self._tree.tag_configure("damaged",   foreground=RED)
        self._tree.tag_configure("current",   foreground=GREEN)
        self._tree.tag_configure("outdated",  foreground=AMBER)
        self._tree.tag_configure("unknown",   foreground=MUTED)
        self._tree.tag_configure("untracked", foreground=PARCH)
        self._tree.tag_configure("disabled",  foreground=MUTED)
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

    # -- data loading --

    def _load(self, force=False):
        self._set_busy(True, "Loading installed mods...")
        if force:
            # Refresh means "look again, actually": damage checks are memoized
            # per install (see verify_mod_files), and this is the explicit
            # user gesture that says re-hash everything.
            _invalidate_verify_memo()
        prev_index = self._index
        def worker():
            try:
                installed = list_installed_mods(self._game_dir)
                untracked = list_untracked_mods(self._game_dir)
            except Exception as e:
                self.after(0, lambda e=e: self._load_failed(e))
                return
            # What's actually installed/untracked is purely local - a mod is
            # still there whether or not the index is reachable right now.
            # The index is only needed for extra context (author, latest
            # version, update state, Change Version), so a fetch failure
            # degrades to showing that context stale/missing rather than
            # hiding the whole table.
            try:
                repos = list_repos(load_cfg())
                index = fetch_indexes(repos, force=force)
            except Exception:
                index = prev_index
            self.after(0, lambda: self._render(index, installed, untracked))
        threading.Thread(target=worker, daemon=True).start()

    def _load_failed(self, err):
        self._set_busy(False, f"Couldn't read installed mods: {err}")

    def _render(self, index, installed, untracked):
        self._index = index
        if self._side == "client":
            self._pinned_ids = {_parse_pin_entry(p)[0] for p in list_pinned(load_cfg())}
        self._rebuild_rows(installed, untracked)
        self._populate_tree()
        self._set_busy(False, "")
        self._refresh_states()

    def _rebuild_rows(self, installed, untracked):
        summaries = {}     # id -> ModSummary with the highest version seen
        for s in self._index:
            cur = summaries.get(s.id)
            if cur is None or _parse_version(s.highest()) > _parse_version(cur.highest()):
                summaries[s.id] = s
        rows = {}
        for meta in installed:
            mod_id = meta.get("id")
            if not mod_id or not meta_matches_side(meta, self._side):
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
            }
        for u in untracked:
            rows[f"u:{u['kind']}:{u['name']}"] = {
                "kind": f"untracked_{u['kind']}", "key": u["name"],
                "name": u["name"], "author": "", "installed": "-", "latest": "-",
                "managed": "Untracked", "source": "-",
                "summary": None, "state": None, "enabled": u["enabled"],
            }
        self._rows = rows

    # -- table rendering --

    def _status_word(self, r):
        if r["kind"] != "managed":
            return "Enabled" if r["enabled"] else "Disabled"
        if r.get("enabled") is False:
            return "Disabled"
        return self._STATE_WORD.get(r["state"], "")

    def _row_tag(self, r):
        if r.get("enabled") is False:
            return "disabled"
        if r["kind"] != "managed":
            return "untracked"
        return r["state"] or "current"

    def _row_values(self, row_id):
        r = self._rows[row_id]
        return (self._status_word(r), r["author"], r["name"],
                r["installed"], r["latest"], r["managed"], r["source"])

    def _populate_tree(self):
        keep = self._selected_id()
        self._tree.delete(*self._tree.get_children())
        for row_id in sorted(self._rows, key=lambda i: self._rows[i]["name"].lower()):
            self._tree.insert("", "end", iid=row_id,
                              values=self._row_values(row_id),
                              tags=(self._row_tag(self._rows[row_id]),))
        if keep and self._tree.exists(keep):
            self._tree.selection_set(keep)
        self._sync_buttons()

    def _refresh_states(self):
        managed_ids = [r["key"] for r in self._rows.values() if r["kind"] == "managed"]
        if not managed_ids:
            return
        index, game_dir = self._index, self._game_dir
        def worker():
            states = {}
            for mod_id in managed_ids:
                try:
                    states[mod_id] = mod_status(game_dir, mod_id, index)
                except Exception:
                    states[mod_id] = "unknown"
            self.after(0, lambda: self._apply_states(states))
        threading.Thread(target=worker, daemon=True).start()

    def _apply_states(self, states):
        for row_id, r in self._rows.items():
            if r["kind"] == "managed" and r["key"] in states:
                r["state"] = states[r["key"]]
                if self._tree.exists(row_id):
                    self._tree.item(row_id, values=self._row_values(row_id),
                                    tags=(self._row_tag(r),))
        self._sync_buttons()

    # -- selection / buttons --

    def _selected_id(self):
        sel = self._tree.selection()
        return sel[0] if sel else None

    def _sync_buttons(self):
        sel = self._selected_id()
        r = self._rows.get(sel) if sel else None
        if self._busy or r is None:
            self._uninstall_btn.config(state="disabled")
            self._toggle_btn.config(state="disabled")
            return
        self._uninstall_btn.config(state="normal" if r["kind"] == "managed" else "disabled")
        self._toggle_btn.config(text="Enable" if not r["enabled"] else "Disable", state="normal")

    # -- actions --

    def _on_right_click(self, event):
        row = self._tree.identify_row(event.y)
        if not row:
            return
        self._tree.selection_set(row)
        self._sync_buttons()
        r = self._rows.get(row)
        if not r:
            return
        menu = self._build_context_menu(row, r)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _build_context_menu(self, row_id, r):
        """Untracked rows are deliberately surfacing-and-toggling only (see
        list_untracked_mods): no Uninstall (we don't own the file/folder well
        enough to promise a clean removal) and no Change Version (no manifest
        to resolve one against)."""
        menu = tk.Menu(self, tearoff=0, bg=SURF, fg=PARCH,
                       activebackground=AMBERDIM, activeforeground=PARCH)
        managed = r["kind"] == "managed"

        menu.add_command(label="Uninstall", command=self._on_uninstall,
                         state="normal" if managed else "disabled")
        menu.add_command(label="Enable" if not r["enabled"] else "Disable",
                         command=self._on_toggle)

        if managed and self._side == "client":
            menu.add_separator()
            # Kept alive on self so Tk's variable trace survives past this
            # call - a BooleanVar with no surviving Python reference can be
            # garbage-collected while the menu is still open.
            self._pin_var = tk.BooleanVar(value=r["key"] in self._pinned_ids)
            menu.add_checkbutton(label="Keep Enabled", variable=self._pin_var,
                                 command=lambda: self._toggle_pin(r["key"]))

        if managed and r["summary"] is not None:
            versions = self._available_versions(r["key"])
            if versions:
                menu.add_separator()
                menu.add_command(label="Change Version",
                                 command=lambda: self._open_version_window(
                                     r["summary"], versions, r["installed"]))

        return menu

    def _available_versions(self, mod_id):
        """Every version of mod_id across every major in the loaded index
        (only the highest major is kept per id above), newest first."""
        versions = set()
        for s in self._index:
            if s.id == mod_id:
                versions.update(s.versions)
        return sorted(versions, key=_parse_version, reverse=True)

    def _open_version_window(self, mod, versions, installed_version):
        repos = list_repos(load_cfg())
        ModVersionWindow(self, mod, versions, installed_version, repos,
                         on_install=lambda v: install_mod_flow(self, mod, v))

    def _on_uninstall(self):
        if self._busy:
            return
        sel = self._selected_id()
        r = self._rows.get(sel) if sel else None
        if not r or r["kind"] != "managed":
            return
        if not messagebox.askyesno("Remove mod",
                f"Remove {r['name']} from your game?\n\nThis deletes its folder from "
                "Mods/. Libraries shared with other installed mods are left in place.",
                parent=self):
            return
        # Asked after the confirm, right before the rmtree: the confirm only
        # blocks this window, so the player can hop to the main launcher and
        # launch the game while it sits open.
        if not confirm_while_game_running(self, self._is_game_running,
                                          f"Removing {r['name']}"):
            return
        self._set_busy(True, f"Removing {r['name']}...")
        game_dir, name, mod_id = self._game_dir, r["name"], r["key"]
        def worker():
            try:
                removed = uninstall_mod(game_dir, mod_id)
                self.after(0, lambda: self._finish(
                    f"{name} removed." if removed else f"{name} was not installed."))
            except Exception as e:
                self.after(0, lambda e=e: self._finish(f"Remove failed: {e}"))
        threading.Thread(target=worker, daemon=True).start()

    def _on_toggle(self):
        if self._busy:
            return
        sel = self._selected_id()
        r = self._rows.get(sel) if sel else None
        if not r:
            return
        disabling = r["enabled"] is True
        # A disable is only a record rename, which survives a running game, so
        # it never asks - a guard on it would just cry wolf. An ENABLE can
        # also pull this mod's pinned libraries back into UserLibs/ (see
        # below), a real write into the game folder, so that direction does.
        # Either way MelonLoader scanned Mods/ at startup, so nothing here
        # reaches the live session.
        if not disabling and not confirm_while_game_running(
                self, self._is_game_running, f"Enabling {r['name']}"):
            return
        self._set_busy(True, f"{'Disabling' if disabling else 'Enabling'} {r['name']}...")
        game_dir, name, kind, key = self._game_dir, r["name"], r["kind"], r["key"]
        def worker():
            try:
                if kind in ("managed", "untracked_folder"):
                    ok = disable_mod(game_dir, key) if disabling else enable_mod(game_dir, key)
                    if ok and not disabling and kind == "managed":
                        # A bare enable is just a record rename, but this mod's
                        # pinned libraries may have been pruned from UserLibs/
                        # while it was disabled (the join flow removes any
                        # library no enabled mod pins). Put them back - cache
                        # first, download if need be - or the mod loads without
                        # them and fails in-game with nothing pointing here.
                        restored = ensure_mod_libraries(
                            game_dir, key,
                            lambda m: self.after(0, lambda m=m: self._status.set(m)))
                        if restored:
                            self.after(0, lambda r=restored: self._status.set(
                                f"Restored {', '.join(r)} for {name}."))
                else:
                    ok = (disable_untracked_dll(game_dir, key) if disabling
                          else enable_untracked_dll(game_dir, key))
                verb = "disabled" if disabling else "enabled"
                msg = f"{name} {verb}." if ok else f"Couldn't {verb[:-1]} {name}."
                self.after(0, lambda: self._finish(msg))
            except Exception as e:
                self.after(0, lambda e=e: self._finish(f"Toggle failed: {e}"))
        threading.Thread(target=worker, daemon=True).start()

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

    def _finish(self, msg):
        self._set_busy(False, msg)
        # The available set didn't change, only what's on disk, so re-scan
        # installed/untracked state locally rather than re-fetching the index.
        self._reconcile_installed()
        self._refresh_states()
        if self._on_change:
            self._on_change()

    def _reconcile_installed(self):
        try:
            installed = list_installed_mods(self._game_dir)
            untracked = list_untracked_mods(self._game_dir)
        except Exception:
            return
        self._rebuild_rows(installed, untracked)
        self._populate_tree()

    def _open_community(self):
        if self._community_win and self._community_win.winfo_exists():
            self._community_win.lift(); return
        self._community_win = CommunityModsWindow(
            self, self._game_dir, side=self._side,
            on_change=lambda: self._load(force=True),
            is_game_running=self._is_game_running)

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
            modlist = export_modlist(self._game_dir, load_cfg(), pin_versions=pin)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(modlist, f, indent=2)
            # Untracked mods are counted separately, not folded into the total:
            # importing this file won't install them, so a single number would
            # promise more than the file can deliver.
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
        index = self._index
        def worker():
            try:
                repos = list_repos(load_cfg())
                plan = import_modlist(modlist, index, repos, self._side)
                self.after(0, lambda: self._confirm_import(plan))
            except Exception as e:
                self.after(0, lambda e=e: self._finish(f"Import failed: {e}"))
        threading.Thread(target=worker, daemon=True).start()

    def _confirm_import(self, plan):
        self._set_busy(False)
        all_mods = plan.roots + plan.dependencies
        to_disable = modlist_import_disables(self._game_dir, plan)
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
        # Listed last and phrased as a to-do, not a failure: nothing here blocks
        # the import, and there's nothing the launcher could do about it anyway.
        # Saying so beats an import that quietly reproduces less than the file
        # describes.
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
                if plan.untracked else
                "This modlist has nothing new to install or disable.", parent=self)
            return
        if not messagebox.askyesno("Import modlist",
                "\n".join(lines) + "\n\nInstall these now?", parent=self):
            return
        # Asked after the confirm, right before the writes begin: the confirm
        # only blocks this window, so the game can have been launched while it
        # sat open.
        if not confirm_while_game_running(self, self._is_game_running,
                                          "Importing a modlist"):
            return

        self._set_busy(True, "Installing...")
        game_dir = self._game_dir
        def worker():
            try:
                apply_import(game_dir, plan,
                             lambda m: self.after(0, lambda: self._status.set(m)))
                msg = f"Imported {len(plan.roots)} mod(s)."
                if to_disable:
                    msg += f" Disabled {len(to_disable)}."
                self.after(0, lambda: self._finish(msg))
            except Exception as e:
                self.after(0, lambda e=e: self._finish(f"Import failed: {e}"))
        threading.Thread(target=worker, daemon=True).start()

    def _set_busy(self, busy, msg=""):
        self._busy = busy
        self._refresh_btn.config(state="disabled" if busy else "normal")
        if msg or not busy:
            self._status.set(msg)
        self._sync_buttons()
