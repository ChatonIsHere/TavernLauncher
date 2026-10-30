"""
Optional TavernKeeper addon: save/replay reusable console command
sequences ("macros"). Delete this whole addons/macros/ folder and the
Macros tab disappears from TavernKeeper with zero changes needed
anywhere in core -- it registers its own tab position (right after
Player Admin) through client.core.tavernkeeper.registry, rather than
being hardcoded into TavernKeeperWindow's tab list.
"""
import os
import json
import tkinter as tk
from tkinter import messagebox

from tavern_shared.theme import (
    BG, SURF, BORDER, AMBER, PARCH, MUTED, MONO,
    _btn, _mk_scrollbar, _wheel_scrolls,
)
from tavern_shared.window_chrome import _set_window_icon, _start_hidden, _finish_dark_window
from tavern_shared.paths import _tavern_data_dir
from client.core.tavernkeeper.registry import register_tab

MACROS_FILE = os.path.join(_tavern_data_dir(), "tavern_macros.json")


def _normalize_macro_commands(raw):
    if raw is None:
        return []
    if isinstance(raw, str):
        items = raw.splitlines()
    elif isinstance(raw, (list, tuple)):
        items = raw
    else:
        items = [raw]
    commands = []
    for item in items:
        text = str(item).strip()
        if text:
            commands.append(text)
    return commands


def _normalize_macros(raw):
    if not isinstance(raw, list):
        raw = []
    macros = []
    for item in raw:
        name = ""
        commands = []
        if isinstance(item, dict):
            name = str(item.get("name", "")).strip()
            commands = _normalize_macro_commands(
                item.get("commands", item.get("command", item.get("lines", [])))
            )
        elif isinstance(item, str):
            name = item.strip()
        else:
            continue

        if not name and commands:
            name = commands[0][:48]
        if not name:
            continue
        macros.append({"name": name, "commands": commands})
    return macros


def _load_macros():
    try:
        with open(MACROS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        raw = []
    return _normalize_macros(raw)


def _save_macros(macros):
    try:
        os.makedirs(os.path.dirname(MACROS_FILE), exist_ok=True)
        with open(MACROS_FILE, "w", encoding="utf-8") as f:
            json.dump(_normalize_macros(macros), f, indent=2)
    except Exception:
        pass


def _build_macros_tab(parent, window):
    tk.Label(parent, text="Save one or more console commands as a reusable macro.",
             bg=BG, fg=MUTED, font=("Segoe UI",9), wraplength=680, justify="left"
    ).pack(anchor="w", padx=8, pady=(10,6))

    row = tk.Frame(parent, bg=BG)
    row.pack(fill="x", padx=8, pady=(0,8))
    _btn(row, "+ Add Macro", lambda: _add_macro(window), "primary",
         font=("Segoe UI",9,"bold"), pady=6, padx=12).pack(side="left")
    _btn(row, "⟳ Refresh", lambda: _refresh_macros(window),
         font=("Segoe UI",9), pady=6, padx=12).pack(side="left", padx=6)
    window._macro_count_var = tk.StringVar(value="0 macros")
    tk.Label(row, textvariable=window._macro_count_var, bg=BG, fg=MUTED,
             font=("Segoe UI",8)).pack(side="right")

    container = tk.Frame(parent, bg=BG)
    container.pack(fill="both", expand=True, padx=8, pady=(0,10))
    window._macro_canvas = tk.Canvas(container, bg=BG, highlightthickness=0)
    sb = _mk_scrollbar(container, window._macro_canvas.yview)
    sb.pack(side="right", fill="y")
    window._macro_canvas.config(yscrollcommand=sb.set)
    window._macro_canvas.pack(side="left", fill="both", expand=True)

    window._macro_list_host = tk.Frame(window._macro_canvas, bg=BG)
    window._macro_window = window._macro_canvas.create_window((0, 0), window=window._macro_list_host, anchor="nw")

    def _sync_scrollregion(_event=None):
        bbox = window._macro_canvas.bbox("all")
        if bbox:
            window._macro_canvas.configure(scrollregion=bbox)

    window._macro_list_host.bind("<Configure>", _sync_scrollregion)
    _wheel_scrolls(window._macro_canvas, window._macro_list_host)
    window._macro_canvas.bind(
        "<Configure>",
        lambda e: window._macro_canvas.itemconfig(window._macro_window, width=e.width),
    )

    # Originally called separately by core right after building this tab --
    # now that this addon is fully self-contained, it triggers its own
    # initial population instead of core needing to know to do this.
    _refresh_macros(window)

def _refresh_macros(window):
    if not hasattr(window, "_macro_list_host"):
        return
    window._macros = _load_macros()
    for child in list(window._macro_list_host.winfo_children()):
        child.destroy()

    count = len(window._macros)
    window._macro_count_var.set(f"{count} macro{'s' if count != 1 else ''}")

    if not window._macros:
        tk.Label(
            window._macro_list_host,
            text="No macros yet. Add one to store a reusable command list.",
            bg=BG, fg=MUTED, font=("Segoe UI",9), wraplength=680, justify="left"
        ).pack(anchor="w", padx=6, pady=12)
        return

    for idx, macro in enumerate(window._macros):
        card = tk.Frame(window._macro_list_host, bg=SURF, highlightbackground=BORDER, highlightthickness=1)
        card.pack(fill="x", padx=2, pady=(0,6))

        left = tk.Frame(card, bg=SURF)
        left.pack(side="left", fill="both", expand=True, padx=10, pady=10)

        tk.Label(left, text=macro.get("name", "Unnamed Macro"), bg=SURF, fg=AMBER,
                 font=("Georgia",10,"bold"), anchor="w", justify="left"
        ).pack(anchor="w")
        commands = macro.get("commands", []) or []
        preview = " ⏎ ".join(commands) if commands else "No commands defined."
        if len(preview) > 220:
            preview = preview[:217] + "…"
        tk.Label(left, text=preview, bg=SURF, fg=PARCH, font=MONO,
                 wraplength=540, justify="left", anchor="w"
        ).pack(anchor="w", pady=(4,0))

        actions = tk.Frame(card, bg=SURF)
        actions.pack(side="right", padx=10, pady=10)
        _btn(actions, "▶ Run", lambda i=idx: _run_macro(window, i), "success",
             font=("Segoe UI",9), pady=5, padx=10).pack(side="left")
        _btn(actions, "✏ Edit", lambda i=idx: _edit_macro(window, i),
             font=("Segoe UI",9), pady=5, padx=10).pack(side="left", padx=6)
        _btn(actions, "🗑 Delete", lambda i=idx: _delete_macro(window, i), "danger",
             font=("Segoe UI",9), pady=5, padx=10).pack(side="left")

def _macro_editor_dialog(window, macro=None):
    win = tk.Toplevel(window)
    _start_hidden(win)
    win.title("Edit Macro" if macro else "Add Macro")
    win.configure(bg=BG)
    win.resizable(True, True)
    _set_window_icon(win)
    win.transient(window)
    win.grab_set()

    tk.Label(win, text="Macro name", bg=BG, fg=PARCH,
             font=("Segoe UI",9,"bold")).pack(anchor="w", padx=16, pady=(14,4))
    name_var = tk.StringVar(value=(macro or {}).get("name", ""))
    name_entry = tk.Entry(win, textvariable=name_var, bg=SURF, fg=PARCH,
                          insertbackground=AMBER, relief="flat",
                          font=("Consolas",10), bd=6)
    name_entry.pack(fill="x", padx=16)

    tk.Label(win, text="Commands (one per line; use {target} for the current player and 'wait <seconds>' to pause)", bg=BG, fg=PARCH,
             font=("Segoe UI",9,"bold")).pack(anchor="w", padx=16, pady=(12,4))
    body = tk.Frame(win, bg=SURF, highlightbackground=BORDER, highlightthickness=1)
    body.pack(fill="both", expand=True, padx=16, pady=(0,10))
    text = tk.Text(body, bg=SURF, fg=PARCH, insertbackground=AMBER,
                   relief="flat", bd=0, wrap="word", font=MONO, height=10)
    scroll = _mk_scrollbar(body, text.yview)
    scroll.pack(side="right", fill="y")
    text.config(yscrollcommand=scroll.set)
    text.pack(side="left", fill="both", expand=True, padx=6, pady=6)
    if macro:
        text.insert("1.0", "\n".join(macro.get("commands", [])))

    result = {}

    def _save(event=None):
        name = name_var.get().strip()
        commands = [line.strip() for line in text.get("1.0", "end").splitlines() if line.strip()]
        if not name:
            messagebox.showinfo("Missing name", "Give the macro a name.", parent=win)
            return
        if not commands:
            messagebox.showinfo("Missing commands", "Enter at least one command.", parent=win)
            return
        result["macro"] = {"name": name, "commands": commands}
        win.destroy()

    btns = tk.Frame(win, bg=BG)
    btns.pack(fill="x", padx=16, pady=(0,14))
    _btn(btns, "Save", _save, "primary",
         font=("Georgia",10,"bold"), pady=8).pack(side="left")
    _btn(btns, "Cancel", win.destroy,
         font=("Segoe UI",9), pady=8, padx=10).pack(side="left", padx=6)

    name_entry.focus_set()
    name_entry.bind("<Return>", lambda e: text.focus_set())
    text.bind("<Control-Return>", _save)
    _finish_dark_window(win)
    window.wait_window(win)
    return result.get("macro")

def _add_macro(window):
    macro = _macro_editor_dialog(window)
    if not macro:
        return
    window._macros.append(macro)
    _save_macros(window._macros)
    _refresh_macros(window)

def _edit_macro(window, idx):
    if idx < 0 or idx >= len(window._macros):
        return
    macro = _macro_editor_dialog(window, window._macros[idx])
    if not macro:
        return
    window._macros[idx] = macro
    _save_macros(window._macros)
    _refresh_macros(window)

def _delete_macro(window, idx):
    if idx < 0 or idx >= len(window._macros):
        return
    macro = window._macros[idx]
    if not messagebox.askyesno(
        "Delete Macro",
        f"Delete macro '{macro.get('name', 'Unnamed Macro')}'?",
        parent=window,
    ):
        return
    window._macros.pop(idx)
    _save_macros(window._macros)
    _refresh_macros(window)

def _run_macro(window, idx):
    if idx < 0 or idx >= len(window._macros):
        return
    if not window._connected or not window._ws_client:
        messagebox.showinfo("Not connected", "Connect to a server first.", parent=window)
        return
    macro = window._macros[idx]
    commands = macro.get("commands", []) or []
    if not commands:
        messagebox.showinfo("Empty macro", "This macro has no commands to run.", parent=window)
        return

    needs_target = any(("{target}" in cmd) or ("{player}" in cmd) or ("{username}" in cmd) for cmd in commands)
    target = window.v_target.get().strip() if hasattr(window, "v_target") else ""
    if needs_target and not target:
        target = window._current_target() or ""
        if not target:
            return

    window._append_log(f"[Macro] {macro.get('name', 'Unnamed Macro')}\n", "ok")

    # time.sleep() here would block the whole Tk mainloop for the
    # duration of every "wait" step -- the entire app would freeze,
    # not just this window. window.after() schedules the remaining
    # commands instead, the same non-blocking pattern already used
    # elsewhere in this file (_rescale_entity_list, _on_spawn_all_saved).
    def run_from(i):
        if i >= len(commands):
            return
        cmd = commands[i]
        if cmd.startswith("wait "):
            wait_text = cmd[5:].strip()
            try:
                delay = max(0.0, float(wait_text))
            except ValueError:
                window._append_log(f"[Macro] Invalid wait time: {wait_text}\n", "err")
                run_from(i + 1)
                return
            window._append_log(f"[Macro] Waiting {wait_text} seconds…\n", "warn")
            window.after(int(delay * 1000), lambda: run_from(i + 1))
            return
        resolved = (cmd.replace("{target}", target)
                      .replace("{player}", target)
                      .replace("{username}", target))
        window._append_log(f"> {resolved}\n", "cyan")
        window._ws_client.send(resolved)
        run_from(i + 1)

    run_from(0)



# The one line that answers "how does it know it goes after Player
# Admin" -- explicitly, right here, rather than anything in core needing
# to know this addon exists.
register_tab("macros", "Macros", _build_macros_tab)
