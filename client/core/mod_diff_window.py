"""The pre-join comparison of a server's mods with this client's, which also
applies the plan and shows its progress."""
import tkinter as tk

from tavern_shared.theme import (
    AMBER, AMBERDIM, BG, BORDER, CYAN, GREEN, MUTED, PARCH, RED, SURF, _btn,
    _scroll_stack, _window_header,
)
from tavern_shared.window_chrome import _enable_dark_titlebar

from tavern_shared.mods.plan import (
    ACTION_ACTIVATE, ACTION_DEACTIVATE, ACTION_DOWNGRADE, ACTION_ENABLE,
    ACTION_INSTALL, ACTION_MATCH, ACTION_MISSING, ACTION_NEEDS_REPO,
    ACTION_SERVER_ONLY, ACTION_SKIPPED, ACTION_UPDATE, render_plan_steps,
)
# One shared tooltip: several widgets can cover the same cell, and separate
# popups let a fast Enter/Leave between them show two or leave one stuck.
_tooltip = {"after_id": None, "win": None, "owner": None}


def _hide_tooltip():
    if _tooltip["after_id"] is not None and _tooltip["owner"] is not None:
        _tooltip["owner"].after_cancel(_tooltip["after_id"])
    if _tooltip["win"] is not None:
        _tooltip["win"].destroy()
    _tooltip.update(after_id=None, win=None, owner=None)


def _attach_tooltip(widget, text):
    def show():
        _tooltip["after_id"] = None
        win = tk.Toplevel(widget)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.geometry(f"+{widget.winfo_rootx()}+{widget.winfo_rooty() + widget.winfo_height() + 4}")
        tk.Label(win, text=text, bg=SURF, fg=PARCH, font=("Segoe UI", 8),
                 wraplength=260, justify="left", padx=8, pady=5,
                 highlightbackground=BORDER, highlightthickness=1).pack()
        _tooltip["win"] = win

    def enter(_event):
        _hide_tooltip()
        _tooltip["owner"] = widget
        _tooltip["after_id"] = widget.after(400, show)

    def leave(_event):
        if _tooltip["owner"] is widget:
            _hide_tooltip()

    widget.bind("<Enter>", enter)
    widget.bind("<Leave>", leave)


class ModDiffWindow(tk.Toplevel):
    """Before Apply, the plan is edited to match the screen: kept rows leave
    plan.to_deactivate and skipped ones leave plan.entries. Afterwards
    declined and pinned_keeps are the choices for the caller to save.
    on_cancel(window) fires once if the window closes without a successful
    apply; apply_attempted tells a decline from a reported failure."""

    _WIDTHS = (280, 90, 90, 190)   # mod, server, you, action
    _TAG_COLORS = {
        "add": GREEN, "chg": AMBER, "del": RED, "same": MUTED, "bad": RED,
        "kept": CYAN, "working": AMBER, "done": GREEN,
    }
    _TAGS = {
        ACTION_INSTALL:     "add",   ACTION_ACTIVATE: "add",  ACTION_ENABLE: "add",
        ACTION_UPDATE:      "chg",   ACTION_DOWNGRADE: "chg",
        ACTION_DEACTIVATE:  "del",   ACTION_MATCH:    "same",
        ACTION_SERVER_ONLY: "same",  ACTION_SKIPPED:  "same",
        ACTION_MISSING:     "bad",   ACTION_NEEDS_REPO: "bad",
    }
    _VERB = {
        ACTION_INSTALL:  "Download",   ACTION_ACTIVATE:  "Install",
        ACTION_ENABLE:   "Enable",     ACTION_UPDATE:    "Update",
        ACTION_DOWNGRADE:"Downgrade",  ACTION_DEACTIVATE:"Deactivate",
        ACTION_MATCH:    "Up to date", ACTION_SERVER_ONLY:"Not needed",
        ACTION_SKIPPED:  "Skipped",
        ACTION_MISSING:  "Unavailable",ACTION_NEEDS_REPO:"Source missing",
    }
    _VERB_ING = {
        ACTION_INSTALL:  "Downloading", ACTION_ACTIVATE:  "Installing",
        ACTION_ENABLE:   "Enabling",    ACTION_UPDATE:    "Updating",
        ACTION_DOWNGRADE:"Downgrading", ACTION_DEACTIVATE:"Deactivating",
    }

    def __init__(self, parent, rows, plan, server_label="this server",
                 apply_action=None, close_on_success=True, on_cancel=None):
        super().__init__(parent)
        self.title("Mod Comparison")
        self.configure(bg=BG)
        self.geometry("760x560")
        self.minsize(660, 440)
        self._rows   = {r.mod_id: r for r in rows}
        self._order  = [r.mod_id for r in rows]
        self._plan   = plan
        self._label  = server_label
        self._keep   = set()    # deactivations overruled
        self._pin    = set()    # kept and to be pinned (a Keep for every server)
        self._skip   = {r.mod_id for r in rows if r.action == ACTION_SKIPPED}
        self.pinned_keeps = []

        self._apply_action     = apply_action
        self._close_on_success = close_on_success
        self._on_cancel_cb     = on_cancel
        self._applying   = False
        self._succeeded  = False
        self._closed     = False
        self.apply_attempted = False
        self.declined = list(self._skip)
        self._progress   = {}     # mod id -> "start" | "done", during an apply
        self._steps_done = 0
        self._steps_total = 0
        self._action_widgets = {}     # mod id -> (label, toggle or None)
        self._name_labels    = {}
        self._segment_labels = {}     # mod id -> {option: label}

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        _enable_dark_titlebar(self)
        self.transient(parent)
        self.grab_set()

    def _build(self):
        _window_header(self, "Mod Comparison", subtitle=self._label)

        tk.Label(self,
            text="Server / You / Action: what this server runs, what's installed here, and "
                 "what applying would do. A toggle means it's your call; no toggle means the "
                 "server decides. Hover a mod name or its Action for more detail.",
            bg=BG, fg=MUTED, font=("Segoe UI",9), wraplength=720, justify="left"
        ).pack(anchor="w", padx=16, pady=(10,6))

        table_wrap = tk.Frame(self, bg=BG)
        table_wrap.pack(fill="both", expand=True, padx=16)

        header = tk.Frame(table_wrap, bg=SURF)
        header.pack(fill="x")
        for text, width in zip(("Mod", "Server", "You", "Action"), self._WIDTHS):
            cell = tk.Frame(header, bg=SURF, width=width, height=26)
            cell.pack(side="left", fill="y"); cell.pack_propagate(False)
            tk.Label(cell, text=text, bg=SURF, fg=AMBER, font=("Segoe UI",9,"bold"),
                     anchor="w").pack(fill="both", padx=6)

        self._rows_frame = _scroll_stack(table_wrap)

        # Packed only once an apply starts.
        self._status = tk.StringVar(value="")
        self._status_lbl = tk.Label(self, textvariable=self._status, bg=BG, fg=MUTED,
                                    font=("Segoe UI",9), wraplength=720,
                                    justify="left", anchor="w")
        self._bar = tk.Canvas(self, bg=SURF, height=6, highlightthickness=0, bd=0)
        self._bar_fill = self._bar.create_rectangle(0, 0, 0, 6, fill=GREEN, width=0)
        self._bar.bind("<Configure>", lambda e: self._draw_bar())

        self._btn_bar = tk.Frame(self, bg=BG)
        self._btn_bar.pack(fill="x", padx=16, pady=(8,12))
        self._apply_btn = _btn(self._btn_bar, "Apply changes", self._on_apply,
                               style="primary", font=("Segoe UI",9), pady=5, padx=14)
        self._apply_btn.pack(side="right")
        self._cancel_btn = _btn(self._btn_bar, "Cancel", self._on_cancel, style="dim",
                                font=("Segoe UI",9), pady=5, padx=14)
        self._cancel_btn.pack(side="right", padx=8)

        self._populate()

    def _draw_bar(self):
        width = max(self._bar.winfo_width(), 1)
        frac = (self._steps_done / self._steps_total) if self._steps_total else 0.0
        self._bar.coords(self._bar_fill, 0, 0, int(width * min(1.0, frac)), 6)

    def _action_text(self, mod_id):
        r = self._rows[mod_id]
        state = self._progress.get(mod_id)
        if state == "done":
            return "Done"
        if state == "start":
            return self._VERB_ING.get(r.action, "Working") + "…"
        if mod_id in self._pin:
            return "Keep active (pinned)"
        if mod_id in self._keep:
            return "Keep active"
        if r.recommended and mod_id in self._skip:
            return "Skip"
        if r.action == ACTION_SKIPPED:
            return "Install"          # was skipped, just turned back on
        return self._proposed_verb(r)

    def _proposed_verb(self, r):
        verb = self._VERB.get(r.action, r.action)
        if (r.action in (ACTION_INSTALL, ACTION_ACTIVATE, ACTION_UPDATE, ACTION_DOWNGRADE)
                and not r.downloads):
            verb += " (cached)"
        return verb

    def _row_tag(self, mod_id):
        state = self._progress.get(mod_id)
        if state:
            return "done" if state == "done" else "working"
        r = self._rows[mod_id]
        if mod_id in self._keep:
            return "kept"
        if r.recommended:
            if mod_id in self._skip:
                return "same" if r.action == ACTION_SKIPPED else "kept"
            return "add" if r.action == ACTION_SKIPPED else self._TAGS.get(r.action, "same")
        return self._TAGS.get(r.action, "same")

    def _row_color(self, mod_id):
        return self._TAG_COLORS.get(self._row_tag(mod_id), MUTED)

    def _choice_options(self, mod_id):
        """(options, current) for an optional row's toggle."""
        r = self._rows[mod_id]
        if r.recommended:
            included = "Install" if r.action == ACTION_SKIPPED else self._proposed_verb(r)
            values = (included, "Skip")
            current = "Skip" if mod_id in self._skip else included
        else:
            values = ("Deactivate", "Keep", "Pin")
            if mod_id in self._pin:
                current = "Pin"
            elif mod_id in self._keep:
                current = "Keep"
            else:
                current = "Deactivate"
        return values, current

    def _populate(self):
        for mod_id in self._order:
            self._mk_row(mod_id)
        blocked = any(r.blocking for r in self._rows.values())
        self._apply_btn.config(
            state="disabled" if blocked or self._apply_action is None else "normal",
            text="Can't apply" if blocked else "Apply changes")

    def _mk_row(self, mod_id):
        r = self._rows[mod_id]
        row = tk.Frame(self._rows_frame, bg=BG)
        row.pack(fill="x")

        name_cell = tk.Frame(row, bg=BG, width=self._WIDTHS[0], height=26)
        name_cell.pack(side="left", fill="y"); name_cell.pack_propagate(False)
        name_lbl = tk.Label(name_cell, text=r.name or r.mod_id, bg=BG,
                            fg=self._row_color(mod_id), font=("Segoe UI",9), anchor="w")
        name_lbl.pack(fill="both", padx=6)
        self._name_labels[mod_id] = name_lbl
        _attach_tooltip(name_cell, r.mod_id)
        _attach_tooltip(name_lbl, r.mod_id)

        for text, width in ((r.server_version or "-", self._WIDTHS[1]),
                            (r.client_version or "-", self._WIDTHS[2])):
            cell = tk.Frame(row, bg=BG, width=width, height=26)
            cell.pack(side="left", fill="y"); cell.pack_propagate(False)
            tk.Label(cell, text=text, bg=BG, fg=MUTED, font=("Segoe UI",9),
                     anchor="w").pack(fill="both", padx=6)

        action_cell = tk.Frame(row, bg=BG, width=self._WIDTHS[3], height=26)
        action_cell.pack(side="left", fill="y"); action_cell.pack_propagate(False)
        action_lbl = tk.Label(action_cell, bg=BG, font=("Segoe UI",9), anchor="w")
        toggle = None
        hover = [action_cell, action_lbl]
        if r.optional:
            toggle = self._mk_toggle(action_cell, mod_id)
            toggle.pack(fill="both")
            hover.append(toggle)
            hover.extend(self._segment_labels[mod_id].values())
        else:
            action_lbl.config(text=self._action_text(mod_id), fg=self._row_color(mod_id))
            action_lbl.pack(fill="both", padx=6)
        self._action_widgets[mod_id] = (action_lbl, toggle)
        if r.note:
            for w in hover:
                _attach_tooltip(w, r.note)

    def _mk_toggle(self, parent, mod_id):
        """Segment labels rather than a dropdown, whose popup fights the theme."""
        values, current = self._choice_options(mod_id)
        wrap = tk.Frame(parent, bg=BG)
        segs = {}
        for text in values:
            lbl = tk.Label(wrap, text=text, font=("Segoe UI",8),
                           padx=6, pady=3, cursor="hand2")
            lbl.pack(side="left", padx=(0,4))
            lbl.bind("<Button-1>",
                    lambda e, mid=mod_id, t=text: self._on_choice_changed(mid, t))
            segs[text] = lbl
        self._segment_labels[mod_id] = segs
        self._paint_toggle(mod_id, current)
        return wrap

    def _paint_toggle(self, mod_id, current):
        for text, lbl in self._segment_labels[mod_id].items():
            active = text == current
            lbl.config(bg=(AMBERDIM if active else SURF),
                      fg=(AMBER if active else MUTED))

    def _on_choice_changed(self, mod_id, choice):
        r = self._rows[mod_id]
        if r.recommended:
            if choice == "Skip":
                self._skip.add(mod_id)
            else:
                self._skip.discard(mod_id)
        else:
            if choice in ("Keep", "Pin"):
                self._keep.add(mod_id)
            else:
                self._keep.discard(mod_id)
            if choice == "Pin":
                self._pin.add(mod_id)
            else:
                self._pin.discard(mod_id)
        self._name_labels[mod_id].config(fg=self._row_color(mod_id))
        self._paint_toggle(mod_id, choice)

    def _on_apply(self):
        if (self._applying or self._apply_action is None
                or any(r.blocking for r in self._rows.values())):
            return
        # A skipped mod's dependencies stay planned in: nothing here knows which
        # root pulled them, and the next join, resolved without it, drops them.
        self._plan.to_deactivate = [m for m in self._plan.to_deactivate
                                    if m not in self._keep]
        self._plan.entries = [e for e in self._plan.entries
                              if e.mod_id not in self._skip]
        self.declined = sorted(self._skip)
        self.pinned_keeps = sorted(self._pin)
        self._begin_apply()
        self._apply_action(self)

    def _begin_apply(self):
        """Turns the table into a progress view. Closing is off meanwhile:
        render_active_set has no abort."""
        self._applying = True
        self.apply_attempted = True
        self._steps_done = 0
        self._steps_total = render_plan_steps(self._plan)
        for mod_id, (label, toggle) in self._action_widgets.items():
            if toggle is not None:
                toggle.pack_forget()
            label.config(text=self._action_text(mod_id), fg=self._row_color(mod_id))
            label.pack(fill="both", padx=6)
        self._apply_btn.config(state="disabled", text="Applying…")
        self._cancel_btn.config(state="disabled")
        self.protocol("WM_DELETE_WINDOW", lambda: None)
        self._status_lbl.config(fg=MUTED)
        self._status.set("Starting…")
        self._status_lbl.pack(anchor="w", fill="x", padx=16, pady=(6,2),
                              before=self._btn_bar)
        self._bar.pack(fill="x", padx=16, pady=(0,4), before=self._btn_bar)
        self._draw_bar()

    # Driven by whoever runs the render, on the Tk thread.

    def set_status(self, message):
        if not self._closed:
            self._status.set(message)

    def set_step(self, item_id, state):
        """render_active_set's on_step; libraries have no row but move the bar."""
        if self._closed:
            return
        if state == "done":
            self._steps_done += 1
            self._draw_bar()
        if item_id in self._rows:
            self._progress[item_id] = state
            self._action_widgets[item_id][0].config(
                text=self._action_text(item_id), fg=self._row_color(item_id))

    def retarget(self, plan):
        """The plan was re-resolved after Apply (an un-declined mod), so the
        bar is re-sized for it."""
        if self._closed:
            return
        self._plan = plan
        self._steps_total = render_plan_steps(plan)
        self._draw_bar()

    def apply_finished(self, ok, message=""):
        """Closes on success with close_on_success, else shows the outcome."""
        if self._closed:
            return
        self._applying = False
        self._succeeded = bool(ok)
        if ok:
            self._steps_done = self._steps_total
            self._draw_bar()
        if ok and self._close_on_success:
            self.destroy()
            return
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self._status_lbl.config(fg=GREEN if ok else RED)
        self._status.set(message or ("Done." if ok else "Couldn't apply the changes."))
        self._apply_btn.config(state="disabled", text="Applied" if ok else "Not applied")
        self._cancel_btn.config(state="normal", text="Close")

    def _on_cancel(self):
        if not self._applying:
            self.destroy()

    def destroy(self):
        first = not self._closed
        self._closed = True
        super().destroy()
        if first and not self._succeeded and self._on_cancel_cb:
            self._on_cancel_cb(self)
