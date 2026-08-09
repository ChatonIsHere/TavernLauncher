"""
The fallback for when the automated install can't work at all.

Some failures are not the launcher's to fix, and retrying them forever is not
a strategy. A corporate/school proxy that needs PAC/WPAD config Python doesn't
evaluate, antivirus that intercepts this specific process's downloads, a
firewall that only allowlists browser traffic, Controlled Folder Access
refusing writes into the game folder -- in every one of those the user's own
browser and File Explorer work perfectly well, and only this app is blocked.

So this window stops pretending and hands the job over: here is the exact file
to fetch, here is the page to fetch it from, here is the exact folder to put it
in, and here are buttons that open both. Nothing here downloads anything.

Every path and URL shown here is imported from the modules that do the
automated install rather than written out again, so the two can't drift into
disagreeing about where a file goes -- a manual-install guide that is subtly
wrong is worse than not having one.
"""
import os
import webbrowser
import tkinter as tk

from tavern_shared.theme import (
    BG, SURF, SURF2, BORDER, AMBER, PARCH, MUTED, CYAN, _btn, _section_label,
)
from tavern_shared.window_chrome import _start_hidden, _finish_dark_window
from tavern_shared.mod_install import (
    MELONLOADER_ZIP_URLS, TAVERNLIB_DOWNLOAD_URL, TAVERNLIB_FILENAME,
    _detect_exe_arch,
)
from tavern_shared.patch import (
    PATCH_DOWNLOAD_URL, PATCH_SOURCE_FILENAME, PATCH_TARGET_FILENAME,
    PATCH_TARGET_SUBDIR,
)

MELONLOADER_RELEASES_PAGE = "https://github.com/LavaGang/MelonLoader/releases/latest"
TAVERNLIB_RELEASES_PAGE   = "https://github.com/ModdingTavern/TavernLib/releases/latest"
PATCH_RELEASES_PAGE       = "https://github.com/ModdingTavern/TavernDefaults/releases/latest"


def _steps_for(component, game_dir, arch):
    """The per-component instructions. Returns (download_name, direct_url,
    page_url, dest_dir, [steps]) — dest_dir is what the Open Folder button
    opens, so it is always the folder the file ends up *in*, never the file."""
    if component == "Patch":
        dest = os.path.join(game_dir, PATCH_TARGET_SUBDIR)
        return (PATCH_SOURCE_FILENAME, PATCH_DOWNLOAD_URL, PATCH_RELEASES_PAGE, dest, [
            f"Download {PATCH_SOURCE_FILENAME} from the release page.",
            f"Rename it to {PATCH_TARGET_FILENAME}.",
            "Open the destination folder below.",
            f"Move it in, replacing the existing {PATCH_TARGET_FILENAME}.",
            "Come back here and press Re-check.",
        ])
    if component == "MelonLoader":
        name = f"MelonLoader.{arch}.zip" if arch else "MelonLoader.x64.zip"
        return (name, MELONLOADER_ZIP_URLS.get(arch or "x64"),
                MELONLOADER_RELEASES_PAGE, game_dir, [
            f"Download {name} from the release page."
            + ("" if arch else "  (Pick x64 unless you know the game is 32-bit.)"),
            "Open the destination folder below — this is the game folder itself.",
            "Open the .zip and drag everything inside it into that folder.",
            "Extract the zip's *contents* here, not the zip folder itself: "
            f"{os.path.join(game_dir, 'version.dll')} must exist when you're done.",
            "Come back here and press Re-check.",
        ])
    if component == "TavernLib":
        dest = os.path.join(game_dir, "Plugins")
        return (TAVERNLIB_FILENAME, TAVERNLIB_DOWNLOAD_URL, TAVERNLIB_RELEASES_PAGE, dest, [
            f"Download {TAVERNLIB_FILENAME} from the release page.",
            "Open the destination folder below.",
            "Move it in, replacing the existing copy if there is one.",
            "Come back here and press Re-check.",
        ])
    raise ValueError(f"No manual-install steps defined for {component!r}")


class ManualInstallWindow(tk.Toplevel):
    """Instructions and shortcuts for installing one component by hand.

    on_recheck is called when the user presses Re-check — Setup passes its own
    _refresh_states, so confirming a manual install goes through exactly the
    same status check as an automated one and can't report a different answer.
    """

    def __init__(self, parent, exe_path, component, reason=None, on_recheck=None):
        super().__init__(parent)
        _start_hidden(self)
        self.title(f"Manual Install — {component}")
        self.configure(bg=BG)
        self.resizable(False, False)
        self._component = component
        self._game_dir = os.path.dirname(exe_path)
        self._on_recheck = on_recheck
        arch = _detect_exe_arch(exe_path)
        (self._name, self._url, self._page,
         self._dest, self._steps) = _steps_for(component, self._game_dir, arch)
        self._build(reason)
        self.update_idletasks()
        self.geometry(f"560x{self.winfo_reqheight()}")
        _finish_dark_window(self)

    def _build(self, reason):
        h = tk.Frame(self, bg=SURF, height=44)
        h.pack(fill="x"); h.pack_propagate(False)
        tk.Label(h, text=f"📄  Manual Install — {self._component}", bg=SURF, fg=AMBER,
                 font=("Georgia", 12, "bold")).pack(side="left", padx=16, pady=8)
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        if reason:
            tk.Label(self, text=reason, bg=BG, fg=MUTED, font=("Segoe UI", 8),
                     wraplength=510, justify="left").pack(anchor="w", padx=20, pady=(10, 0))

        tk.Label(self,
            text="The automatic install couldn't complete, so here is how to do it "
                 "by hand. Your browser and File Explorer aren't subject to whatever "
                 "blocked the launcher, so this works even when the buttons in Setup "
                 "don't.",
            bg=BG, fg=MUTED, font=("Segoe UI", 9), wraplength=510, justify="left"
        ).pack(anchor="w", padx=20, pady=(10, 8))

        _section_label(self, "STEPS")
        steps = tk.Frame(self, bg=BG)
        steps.pack(fill="x", padx=20, pady=(0, 6))
        for i, step in enumerate(self._steps, start=1):
            row = tk.Frame(steps, bg=BG)
            row.pack(fill="x", pady=1)
            tk.Label(row, text=f"{i}.", bg=BG, fg=AMBER, font=("Segoe UI", 9, "bold"),
                     width=2, anchor="ne").pack(side="left")
            tk.Label(row, text=step, bg=BG, fg=PARCH, font=("Segoe UI", 9),
                     wraplength=470, justify="left").pack(side="left", anchor="w")

        _section_label(self, "FILE")
        self._path_box("Download", self._name)
        self._path_box("Goes in", self._dest)

        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=20, pady=(12, 6))
        _btn(bar, "🌐 Open Release Page", self._open_page,
             style="primary", font=("Segoe UI", 9), pady=6, padx=10).pack(side="left")
        _btn(bar, "📋 Copy Link", self._copy_link,
             font=("Segoe UI", 9), pady=6, padx=10).pack(side="left", padx=6)
        _btn(bar, "📂 Open Folder", self._open_dest,
             font=("Segoe UI", 9), pady=6, padx=10).pack(side="left")

        self._status = tk.StringVar(value="")
        tk.Label(self, textvariable=self._status, bg=BG, fg=CYAN, font=("Segoe UI", 8),
                 wraplength=510, justify="left").pack(anchor="w", padx=20)

        tk.Frame(self, bg=BORDER, height=1).pack(fill="x", pady=(8, 0))
        foot = tk.Frame(self, bg=BG)
        foot.pack(fill="x", padx=20, pady=10)
        _btn(foot, "✓ Re-check", self._recheck, style="primary",
             font=("Segoe UI", 9, "bold"), pady=6, padx=14).pack(side="left")
        _btn(foot, "Close", self.destroy,
             font=("Segoe UI", 9), pady=6, padx=14).pack(side="right")

    def _path_box(self, label, shown):
        """A label plus a selectable, read-only path. Selectable because the
        whole point of this window is getting a value out of the launcher and
        into something else -- a path the user can't copy is a path they have
        to retype, and these are long enough to get wrong."""
        wrap = tk.Frame(self, bg=BG)
        wrap.pack(fill="x", padx=20, pady=2)
        tk.Label(wrap, text=label, bg=BG, fg=MUTED, font=("Segoe UI", 8),
                 width=9, anchor="w").pack(side="left")
        entry = tk.Entry(wrap, bg=SURF2, fg=PARCH, font=("Consolas", 8),
                         relief="flat", insertbackground=PARCH,
                         highlightbackground=BORDER, highlightthickness=1)
        entry.insert(0, shown)
        # readonly rather than disabled: still selectable and copyable, which
        # disabled is not.
        entry.config(state="readonly", readonlybackground=SURF2)
        entry.pack(side="left", fill="x", expand=True, ipady=3)

    def _open_page(self):
        webbrowser.open(self._page)
        self._status.set("Opened the release page in your browser.")

    def _copy_link(self):
        if not self._url:
            self._status.set("No direct link for this one — use the release page.")
            return
        self.clipboard_clear()
        self.clipboard_append(self._url)
        self._status.set("Direct download link copied to the clipboard.")

    def _open_dest(self):
        """Creates the folder first if it doesn't exist. Only reachable for
        Plugins/, which the automated install would also have created -- if
        the *game* folder is missing, that's a wrong exe path and no amount of
        creating directories helps, so that case is reported rather than
        papered over."""
        if not os.path.isdir(self._game_dir):
            self._status.set(
                f"The game folder doesn't exist: {self._game_dir}\n"
                "Re-browse to the game .exe on the main screen.")
            return
        try:
            os.makedirs(self._dest, exist_ok=True)
        except OSError as err:
            self._status.set(f"Couldn't create {self._dest} — {err}")
            return
        try:
            os.startfile(self._dest)  # Windows-only, same as the rest of this app
        except (OSError, AttributeError) as err:
            self._status.set(f"Couldn't open the folder — {err}\nPath: {self._dest}")
            return
        self._status.set("Opened the destination folder.")

    def _recheck(self):
        if self._on_recheck:
            self._on_recheck()
        self._status.set("Re-checked — see the Setup window for the result.")
