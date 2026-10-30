"""
Shared visual theme + basic Tk widget helpers, used identically by both
the client and server launchers. Extracted from what were previously two
near-identical (and slowly drifting) copies in att_client.py/att_server.py --
this file uses the client's versions throughout, since a diff showed the
client side had picked up a few small improvements (hscroll on _mk_tree,
the "dim" button style, wraplength on _hint) the server side never got.
Nothing here is behavior-changing for either app; this is a pure move.
"""
import tkinter as tk
from tkinter import ttk

BG       = "#1a1210"

SURF     = "#241c17"

SURF2    = "#2e2218"

BORDER   = "#4a3828"

AMBER    = "#e8a840"

AMBERDIM = "#8a5e1a"

PARCH    = "#f0e6cc"

MUTED    = "#8a7a62"

GREEN    = "#6aaa72"

RED      = "#c45c5c"

CYAN     = "#6ab0aa"

MONO     = ("Consolas", 9)


def _divider(parent):
    f = tk.Frame(parent, bg=BG)
    f.pack(fill="x", padx=20, pady=5)
    tk.Frame(f, bg=BORDER, height=1).pack(side="left", fill="x", expand=True, pady=4)
    tk.Label(f, text=" ✦ ", bg=BG, fg=AMBERDIM, font=("Georgia",9)).pack(side="left")
    tk.Frame(f, bg=BORDER, height=1).pack(side="left", fill="x", expand=True, pady=4)


def _section_label(parent, text):
    tk.Label(parent, text=text, bg=BG, fg=MUTED,
             font=("Georgia",8,"bold")).pack(anchor="w", padx=22, pady=(7,3))


def _field(parent):
    f = tk.Frame(parent, bg=SURF, highlightbackground=BORDER, highlightthickness=1)
    f.pack(fill="x", padx=20, pady=(0,3))
    return f


def _hint(parent, text):
    lbl = tk.Label(parent, text=text, bg=BG, fg=MUTED, justify="left",
                   font=("Segoe UI",8))
    lbl.pack(anchor="w", padx=22, pady=(0,2))
    def _update_wrap(event):
        # A toplevel also receives Configure events from every widget in it.
        if event.widget is not parent:
            return
        lbl.config(wraplength=max(100, event.width - 44))
    parent.bind("<Configure>", _update_wrap, add="+")
    return lbl


def _btn(parent, text, cmd, style="normal", **kw):
    colors = {
        "normal":  (SURF2, PARCH, AMBERDIM, AMBER),
        "primary": ("#3d2a0a", AMBER, "#5a3d0e", "#ffd080"),
        "danger":  ("#3d1010","#e88080","#5a1818","#ffaaaa"),
        "success": ("#1a3d1e","#a8d8a0","#2a5e2e","#c8f0c0"),
        "dim":     (SURF,     MUTED,  SURF2,   PARCH),
        # as the Discord and Token buttons in the launcher header
        "badge":   (SURF2,    AMBER,  AMBERDIM, "#ffd080"),
    }[style]
    return tk.Button(parent, text=text, bg=colors[0], fg=colors[1],
                     activebackground=colors[2], activeforeground=colors[3],
                     # Tk's default is nearly invisible on this palette.
                     disabledforeground=MUTED,
                     relief="flat", bd=0, cursor="hand2", command=cmd, **kw)


def _fix_combobox_popdown_colors(combo, bg, fg, select_bg, select_fg):
    """Forces the ttk.Combobox dropdown list's colors directly via Tcl,
    bypassing the option database entirely. option_add("*TCombobox*
    Listbox...") is the commonly-documented approach, but is unreliable
    across Tk versions/platforms in practice -- it silently loses to the
    OS theme's own white background often enough that this app's
    comboboxes have apparently never actually had this working anywhere.

    On Windows, the popdown is a native-themed `ComboboxPopdown` widget
    (confirmed via direct diagnostic against a real install: Tcl/Tk
    8.6.15, path always <popdown>.f.l for the actual Listbox) -- its
    "Post" step appears to reassert the OS default listbox colors every
    time it's actually shown, which is almost certainly why an earlier
    version of this fix (reapplying after a guessed fixed delay
    following the click that opens it) didn't reliably stick: it was
    racing that reset instead of reliably running after it. Binding
    directly to the popdown's own <Map> event -- which fires exactly
    when it becomes visible, not an approximation of when that might be
    -- avoids that race entirely.
    """
    def _configure_listbox():
        try:
            popdown = combo.tk.eval(f"ttk::combobox::PopdownWindow {combo}")
            combo.tk.call(f"{popdown}.f.l", "configure",
                           "-background", bg, "-foreground", fg,
                           "-selectbackground", select_bg, "-selectforeground", select_fg)
        except tk.TclError:
            pass

    def _on_first_open(event=None):
        # The popdown doesn't exist as a real widget until the combobox
        # has been opened at least once -- this runs on every click, but
        # the <Map> binding below is only actually attached the first
        # time (Tcl's own bind command harmlessly no-ops on a duplicate
        # identical binding, but there's no need to re-add it each time).
        try:
            popdown = combo.tk.eval(f"ttk::combobox::PopdownWindow {combo}")
        except tk.TclError:
            return
        if not getattr(combo, "_popdown_map_bound", False):
            cmd = combo.register(_configure_listbox)
            combo.tk.call("bind", popdown, "<Map>", f"+{cmd}")
            combo._popdown_map_bound = True
        _configure_listbox()

    combo.bind("<Button-1>", _on_first_open, add="+")
    combo.after_idle(_configure_listbox)


def _mk_combobox(parent, var, values):
    style = ttk.Style()
    style.configure("Tav.TCombobox",
                    fieldbackground=SURF, background=SURF2,
                    foreground=PARCH, selectbackground=SURF,
                    selectforeground=PARCH, arrowcolor=AMBERDIM, borderwidth=0)
    style.map("Tav.TCombobox",
              fieldbackground=[("readonly",SURF)],
              foreground=[("readonly",PARCH)],
              selectbackground=[("readonly",SURF)],
              selectforeground=[("readonly",PARCH)])
    parent.option_add("*TCombobox*Listbox.background",       SURF)
    parent.option_add("*TCombobox*Listbox.foreground",       PARCH)
    parent.option_add("*TCombobox*Listbox.selectBackground", AMBERDIM)
    parent.option_add("*TCombobox*Listbox.selectForeground", "#ffd080")
    cb = ttk.Combobox(parent, textvariable=var, values=values,
                      state="readonly", font=("Consolas",10), style="Tav.TCombobox")
    cb.pack(fill="x", ipady=4, padx=6, pady=6)
    _fix_combobox_popdown_colors(cb, SURF, PARCH, AMBERDIM, "#ffd080")
    return cb


def _mk_scrollbar(parent, command, orient="vertical"):
    """A ttk scrollbar styled to match the dark theme.
    Plain tk.Scrollbar renders using native Windows visual styles and ignores
    bg/troughcolor there, which is why scrollbars stayed white — ttk under the
    'clam' theme draws its own elements instead, so our colors actually apply."""
    style = ttk.Style()
    name = "Tav.Vertical.TScrollbar" if orient == "vertical" else "Tav.Horizontal.TScrollbar"
    style.configure(name, background=SURF2, troughcolor=BG, bordercolor=BORDER,
                    arrowcolor=AMBERDIM, darkcolor=SURF2, lightcolor=SURF2, relief="flat")
    style.map(name, background=[("active", AMBERDIM), ("pressed", AMBERDIM)],
              arrowcolor=[("pressed", "#ffd080")])
    sb = ttk.Scrollbar(parent, orient=orient, command=command, style=name)
    return sb


def _mk_tree(parent, cols, widths, height=8, hscroll=False):
    style = ttk.Style()
    style.configure("Tav.Treeview", background=SURF, fieldbackground=SURF,
                    foreground=PARCH, rowheight=26, borderwidth=0)
    style.configure("Tav.Treeview.Heading", background=SURF2, foreground=AMBER,
                    font=("Georgia",9,"bold"))
    style.map("Tav.Treeview",
              background=[("selected",AMBERDIM)],
              foreground=[("selected","#ffd080")])
    f = tk.Frame(parent, bg=SURF, highlightbackground=BORDER, highlightthickness=1)
    f.pack(fill="both", expand=True)

    hsb = None
    if hscroll:
        # Pack the horizontal scrollbar at the bottom first so it claims its
        # space before the tree body below fills the rest — reversing this
        # order would let the tree body crowd the scrollbar out entirely.
        hsb = _mk_scrollbar(f, None, "horizontal")
        hsb.pack(side="bottom", fill="x")

    body = tk.Frame(f, bg=SURF)
    body.pack(fill="both", expand=True, padx=2, pady=2)

    tree = ttk.Treeview(body, columns=cols, show="headings",
                        selectmode="browse", height=height, style="Tav.Treeview")
    for col, w in zip(cols, widths):
        tree.heading(col, text=col.replace("_"," ").title())
        # With a horizontal scrollbar, columns should keep their exact width
        # and overflow into scroll range rather than being squeezed to fit —
        # that squeezing is exactly what made columns unreadable before.
        tree.column(col, width=w, minwidth=w, stretch=not hscroll, anchor="w")

    vsb = _mk_scrollbar(body, tree.yview, "vertical")
    vsb.pack(side="right", fill="y")
    tree.pack(side="left", fill="both", expand=True)
    tree.config(yscrollcommand=vsb.set)
    if hsb is not None:
        hsb.config(command=tree.xview)
        tree.config(xscrollcommand=hsb.set)
    return tree


def _window_header(parent, title, subtitle=None):
    """A dialog's title strip, returned for buttons to pack into."""
    h = tk.Frame(parent, bg=SURF, height=44)
    h.pack(fill="x"); h.pack_propagate(False)
    tk.Label(h, text=title, bg=SURF, fg=AMBER,
             font=("Georgia",12,"bold")).pack(side="left", padx=16, pady=8)
    if subtitle:
        tk.Label(h, text=subtitle, bg=SURF, fg=MUTED,
                 font=("Segoe UI",9)).pack(side="left", pady=8)
    tk.Frame(parent, bg=BORDER, height=1).pack(fill="x")
    return h


def _scroll_stack(parent):
    """A scrolling frame to pack rows into, for lists a Treeview can't draw."""
    frame = tk.Frame(parent, bg=BG)
    frame.pack(fill="both", expand=True)
    canvas = tk.Canvas(frame, bg=BG, highlightthickness=0)
    vsb = _mk_scrollbar(frame, canvas.yview)
    vsb.pack(side="right", fill="y")
    canvas.config(yscrollcommand=vsb.set)
    canvas.pack(side="left", fill="both", expand=True)
    inner = tk.Frame(canvas, bg=BG)
    window = canvas.create_window((0, 0), window=inner, anchor="nw")
    inner.bind("<Configure>",
               lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.bind("<Configure>", lambda e: canvas.itemconfig(window, width=e.width))
    _wheel_scrolls(canvas, inner)
    return inner


# Widgets whose own class bindings already scroll them on the wheel.
_NATIVE_WHEEL = ("Text", "Listbox", "Treeview")


def _wheel_steps(event):
    """Units to scroll for one wheel event: Windows' delta is a multiple of
    120 (less from some touchpads, still one step); X11 sends buttons 4/5."""
    if getattr(event, "num", None) == 4:
        return -1
    if getattr(event, "num", None) == 5:
        return 1
    return int(-event.delta / 120) or (-1 if event.delta > 0 else 1)


def _wheel_scroll(event, start=None):
    """Scrolls the innermost area under the pointer that can scroll. Tk
    sends the wheel to the widget under the pointer, and a row's labels and
    buttons sit on top of its canvas, so this walks up from that widget
    (or from `start`)."""
    w = start if start is not None else event.widget
    if isinstance(w, str):   # Tk-internal, e.g. a combobox's dropdown list
        return
    steps = _wheel_steps(event)
    while w is not None:
        inner = getattr(w, "_tav_wheel_inner", None)
        try:
            if inner is not None:
                # Or a short list scrolls off into empty space.
                if inner.winfo_height() > w.winfo_height():
                    w.yview_scroll(steps, "units")
                    return
            elif w.winfo_class() == "TCombobox":
                return       # its own binding stepped the value
            elif w.winfo_class() in _NATIVE_WHEEL and tuple(w.yview()) != (0.0, 1.0):
                return       # it scrolled itself
        except tk.TclError:
            return
        w = w.master


def _wheel_scrolls(canvas, inner):
    """Makes the wheel scroll `canvas` anywhere over `inner`, the frame it
    scrolls, rows added later included. One handler on the "all" tag looks
    up the area under the pointer (_wheel_scroll), so in nested lists the
    one the pointer is over scrolls. Don't bind_all the wheel anywhere
    else: without add="+" that replaces this handler app-wide."""
    canvas._tav_wheel_inner = inner
    root = canvas._root()
    if not getattr(root, "_tav_wheel_bound", False):
        root._tav_wheel_bound = True
        root.bind_all("<MouseWheel>", _wheel_scroll, add="+")
        # Only X11 sends the wheel as buttons; Tk 8.6 on Windows reports a
        # mouse's back/forward side buttons as 4/5.
        if root.tk.call("tk", "windowingsystem") == "x11":
            root.bind_all("<Button-4>", _wheel_scroll, add="+")
            root.bind_all("<Button-5>", _wheel_scroll, add="+")
