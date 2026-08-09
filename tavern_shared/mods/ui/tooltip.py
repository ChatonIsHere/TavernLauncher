"""A single shared hover tooltip. One at a time, so it is module state."""
import tkinter as tk

from tavern_shared.theme import BORDER, PARCH, SURF


_tooltip_state = {"after_id": None, "win": None, "owner": None}


def _hide_tooltip():
    if _tooltip_state["after_id"] is not None and _tooltip_state["owner"] is not None:
        _tooltip_state["owner"].after_cancel(_tooltip_state["after_id"])
    _tooltip_state["after_id"] = None
    if _tooltip_state["win"] is not None:
        _tooltip_state["win"].destroy()
    _tooltip_state["win"] = None
    _tooltip_state["owner"] = None


def _attach_tooltip(widget, text):
    """A small delayed popup on hover - tk has no built-in tooltip, so this is
    the minimal version: appears after a beat over the widget, gone the
    instant the pointer leaves. The pending timer and the popup itself live
    in shared module state rather than a per-widget closure, so entering any
    tooltip-bound widget always clears whatever another one left behind
    first. That matters because several widgets can cover the exact same
    row (a fixed-width cell and the label filling it, say) - each tracking
    its own popup independently let a fast Enter/Leave between them show two
    at once or leave one stuck open."""
    def _show():
        _tooltip_state["after_id"] = None
        win = tk.Toplevel(widget)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        x = widget.winfo_rootx()
        y = widget.winfo_rooty() + widget.winfo_height() + 4
        win.geometry(f"+{x}+{y}")
        tk.Label(win, text=text, bg=SURF, fg=PARCH, font=("Segoe UI", 8),
                 wraplength=260, justify="left", padx=8, pady=5,
                 highlightbackground=BORDER, highlightthickness=1
        ).pack()
        _tooltip_state["win"] = win

    def _enter(_event):
        _hide_tooltip()
        _tooltip_state["owner"] = widget
        _tooltip_state["after_id"] = widget.after(400, _show)

    def _leave(_event):
        if _tooltip_state["owner"] is widget:
            _hide_tooltip()

    widget.bind("<Enter>", _enter)
    widget.bind("<Leave>", _leave)
