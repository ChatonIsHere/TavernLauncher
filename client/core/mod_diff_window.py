"""The pre-join reconciliation diff, with per-row toggles. Client only -- a
server never joins anything."""
import tkinter as tk

from tavern_shared.theme import (
    AMBER, AMBERDIM, BG, BORDER, CYAN, GREEN, MUTED, RED, SURF, _btn,
    _mk_scrollbar,
)
from tavern_shared.window_chrome import _enable_dark_titlebar

from tavern_shared.mods.plan import (
    ACTION_ACTIVATE, ACTION_DEACTIVATE, ACTION_DOWNGRADE, ACTION_ENABLE,
    ACTION_INSTALL, ACTION_MATCH, ACTION_MISSING, ACTION_NEEDS_REPO,
    ACTION_SERVER_ONLY, ACTION_SKIPPED, ACTION_UPDATE, render_plan_steps,
)
from tavern_shared.mods.ui.tooltip import _attach_tooltip


class ModDiffWindow(tk.Toplevel):
    """Side-by-side comparison of what a server runs against what's installed
    here, shown before anything is applied. Replaces a flat "will install / will
    deactivate" list with the actual two sides and a per-mod verdict, so it's
    clear WHY each change is proposed and what the server is actually asking for.

    Also where applying happens. Given an apply_action, pressing Apply turns the
    same table into a progress view - each row saying what's happening to it,
    a bar underneath - and the window stays up until the work is done. The
    outcome is reported in the window rather than a popup: with
    close_on_success it disappears and the caller carries on (the join case),
    otherwise it stays with the result on screen (the sync case).

    Without an apply_action it's a plain confirm dialog: the caller waits, then
    reads `.result` (True to apply).

    The plan is edited to match the screen before anything is rendered, so what
    gets applied is what was shown: a deactivation the user keeps is dropped from
    plan.to_deactivate, and a recommended mod the user skips is dropped from
    plan.entries. `declined` afterwards is the full set of recommended mods
    turned off for this server, for the caller to remember.
    on_cancel(window) fires exactly once if the window closes without a
    successful apply, so a caller waiting on it isn't left hanging. Check
    `.apply_attempted` there to tell a plain decline from a close after a
    failure that has already been reported."""

    _WIDTHS = (280, 90, 90, 190)   # mod, server, you, action

    # Row colour per action, keyed by an abstract tag name resolved to an
    # actual colour in _build. Reads as a diff at a glance: additions green,
    # removals red, version changes amber, problems red, quiet rows muted.
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
    # What a row says while it's being worked on, so the table reads as live
    # progress instead of a static proposal.
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
        # Both directions a row can be overruled. _keep: deactivations the user
        # wants to hold on to. _skip: recommended mods the user doesn't want.
        # A row starts in _skip if it came in already declined for this server.
        # _pin: kept rows the user wants kept EVERYWHERE - the durable version
        # of _keep. A plain Keep only survives until the next join re-proposes
        # the deactivation; a pin unions the mod into every future plan (Mod
        # Manager's Keep Enabled), so it stops being offered for deactivation
        # at all. The caller reads .pinned_keeps after an apply and writes the
        # actual pins - this window never touches config itself.
        self._keep   = set()
        self._pin    = set()
        self._skip   = {r.mod_id for r in rows if r.action == ACTION_SKIPPED}
        self.pinned_keeps = []
        self.result  = False

        self._apply_action     = apply_action
        self._close_on_success = close_on_success
        self._on_cancel_cb     = on_cancel
        self._applying   = False
        self._closed     = False
        self.apply_attempted = False   # Apply was pressed; a close isn't a decline
        self.declined = list(self._skip)   # kept current on every apply
        self._progress   = {}     # mod id -> "start" | "done", during an apply
        self._steps_done = 0
        self._steps_total = 0
        # mod id -> (action_cell, action_label, toggle_frame_or_None); the
        # toggle is None for a non-optional row, which never had a choice.
        self._action_widgets = {}
        self._name_labels    = {}     # mod id -> its coloured name label
        self._segment_labels = {}     # mod id -> {option text: its segment label}

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        _enable_dark_titlebar(self)
        self.transient(parent)
        self.grab_set()

    def _build(self):
        h = tk.Frame(self, bg=SURF, height=44)
        h.pack(fill="x"); h.pack_propagate(False)
        tk.Label(h, text="Mod Comparison", bg=SURF, fg=AMBER,
                 font=("Georgia",12,"bold")).pack(side="left", padx=16, pady=8)
        tk.Label(h, text=self._label, bg=SURF, fg=MUTED,
                 font=("Segoe UI",9)).pack(side="left", pady=8)
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        tk.Label(self,
            text="Server / You / Action: what this server runs, what's installed here, and "
                 "what applying would do. A toggle means it's your call; no toggle means the "
                 "server decides. Hover a mod name or its Action for more detail.",
            bg=BG, fg=MUTED, font=("Segoe UI",9), wraplength=720, justify="left"
        ).pack(anchor="w", padx=16, pady=(10,6))

        # Colour per abstract tag, resolved against the shared palette.
        self._tag_colors = {
            "add": GREEN, "chg": AMBER, "del": RED, "same": MUTED, "bad": RED,
            "kept": CYAN, "working": AMBER, "done": GREEN,
        }

        table_wrap = tk.Frame(self, bg=BG)
        table_wrap.pack(fill="both", expand=True, padx=16)

        header = tk.Frame(table_wrap, bg=SURF)
        header.pack(fill="x")
        for text, width in zip(("Mod", "Server", "You", "Action"), self._WIDTHS):
            cell = tk.Frame(header, bg=SURF, width=width, height=26)
            cell.pack(side="left", fill="y"); cell.pack_propagate(False)
            tk.Label(cell, text=text, bg=SURF, fg=AMBER, font=("Segoe UI",9,"bold"),
                     anchor="w").pack(fill="both", padx=6)

        # A Treeview can't host a live control per row, so the table itself is
        # a scrollable stack of row frames instead - each optional row gets
        # its own two-way toggle rather than a shared button whose meaning
        # changes depending what's selected.
        canvas_frame = tk.Frame(table_wrap, bg=BG)
        canvas_frame.pack(fill="both", expand=True)
        canvas = tk.Canvas(canvas_frame, bg=BG, highlightthickness=0)
        vsb = _mk_scrollbar(canvas_frame, canvas.yview)
        vsb.pack(side="right", fill="y")
        canvas.config(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        self._canvas = canvas
        self._rows_frame = tk.Frame(canvas, bg=BG)
        window = canvas.create_window((0, 0), window=self._rows_frame, anchor="nw")
        self._rows_frame.bind("<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(window, width=e.width))
        def _mousewheel(event):
            # Without this guard the canvas will still happily scroll a
            # screenful of nothing - scrollregion can end up briefly stale
            # (e.g. mid-rebuild), and yview_scroll doesn't check content
            # height on its own before moving the view.
            if self._rows_frame.winfo_height() <= canvas.winfo_height():
                return
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind("<MouseWheel>", _mousewheel)
        self._rows_frame.bind("<MouseWheel>", _mousewheel)

        # Progress line and bar. Built now so they keep their place in the
        # layout, packed only once an apply starts (see _begin_apply).
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
            return "Install"          # was skipped, user has just turned it back on
        verb = self._VERB.get(r.action, r.action)
        # Say which rows cost a download and which are just a file move out
        # of the cache: it's the difference between a moment and a wait.
        if r.action in (ACTION_INSTALL, ACTION_ACTIVATE, ACTION_UPDATE, ACTION_DOWNGRADE):
            verb += "" if r.downloads else " (cached)"
        return verb

    def _row_tag(self, mod_id):
        state = self._progress.get(mod_id)
        if state:
            return "done" if state == "done" else "working"
        r = self._rows[mod_id]
        if mod_id in self._keep:
            return "kept"
        if r.recommended:
            # Overruled either way reads as a deliberate choice, not a change.
            if mod_id in self._skip:
                return "same" if r.action == ACTION_SKIPPED else "kept"
            return "add" if r.action == ACTION_SKIPPED else self._TAGS.get(r.action, "same")
        return self._TAGS.get(r.action, "same")

    def _row_color(self, mod_id):
        return self._tag_colors.get(self._row_tag(mod_id), MUTED)

    def _choice_options(self, mod_id):
        """(values, current) for an optional row's toggle. 'included' is
        whichever option the plan proposes by default - install for a
        recommendation, deactivate for an extra the server doesn't use;
        'current' is the separate, actual pending choice. A deactivation row
        gets a third choice, Pin: Keep for this apply AND remember it as an
        always-on pin, so the next join stops proposing the deactivation
        (the hover note explains it; same effect as Mod Manager's Keep
        Enabled)."""
        r = self._rows[mod_id]
        if r.recommended:
            if r.action == ACTION_SKIPPED:
                included = "Install"
            else:
                included = self._VERB.get(r.action, r.action)
                if r.action in (ACTION_INSTALL, ACTION_ACTIVATE,
                                ACTION_UPDATE, ACTION_DOWNGRADE):
                    included += "" if r.downloads else " (cached)"
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
        for child in self._rows_frame.winfo_children():
            child.destroy()
        self._action_widgets = {}
        self._name_labels = {}
        self._segment_labels = {}
        for mod_id in self._order:
            self._mk_row(mod_id)
        # The <Configure> binding usually keeps the scrollregion current, but
        # it fires off Tk's own idle loop - forcing it here too means a
        # rebuild never leaves a stale, too-tall region behind that would let
        # the wheel/scrollbar move the view into empty space.
        self._rows_frame.update_idletasks()
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))
        # A required mod nobody can supply can't be resolved by pressing
        # Apply, so the button says so rather than failing after the fact.
        blocked = any(r.blocking for r in self._rows.values())
        self._apply_btn.config(state="disabled" if blocked else "normal",
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
        # The display name is friendlier; the id is what you'd actually need
        # to go looking for it (a manifest file, a repo listing), so it's a
        # hover away rather than gone.
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
        self._action_widgets[mod_id] = (action_cell, action_lbl, toggle)
        # r.note is the longer explanation (why it's listed, a pin conflict,
        # etc.) that used to sit under the row as its own line - now a hover
        # away over Action instead, on whichever widget is actually visible
        # there (a toggle's segments included, since they cover the cell).
        if r.note:
            for w in hover:
                _attach_tooltip(w, r.note)

    def _mk_toggle(self, parent, mod_id):
        """A two-way toggle for an optional row, in place of a dropdown: two
        small labels side by side, the live choice lit and the other dim -
        click either to switch. Nothing pops open, so there's no separate
        listbox popup fighting the dark theme, and the current choice reads
        without needing to click anything first."""
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
            # "Skip" overrides the plan's default; the other option means don't.
            if choice == "Skip":
                self._skip.add(mod_id)
            else:
                self._skip.discard(mod_id)
        else:
            # Keep and Pin both hold the mod active; Pin additionally marks it
            # to be remembered as an always-on pin when the plan is applied.
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
        if self._applying or any(r.blocking for r in self._rows.values()):
            return
        # Bring the plan in line with the screen, so what gets rendered is what
        # was shown. Kept mods come out of to_deactivate and stay where they are;
        # skipped recommendations come out of entries and are never fetched.
        #
        # A skipped mod's own dependencies stay planned in, since nothing here
        # knows which root pulled which dependency. They're harmless (installed
        # and enabled, just unused) and the next join drops them on its own,
        # having resolved the active set without that root in the first place.
        self._plan.to_deactivate = [m for m in self._plan.to_deactivate
                                    if m not in self._keep]
        self._plan.entries = [e for e in self._plan.entries
                              if e.mod_id not in self._skip]
        # What the caller should remember for this server, including choices
        # taken back: a row toggled off _skip drops out of here too. And the
        # kept rows the user asked to make durable - the caller turns each
        # into an always-on pin alongside saving the declines.
        self.declined = sorted(self._skip)
        self.pinned_keeps = sorted(self._pin)
        if self._apply_action is None:
            self.result = True
            self.destroy()
            return
        self._begin_apply()
        self._apply_action(self)

    def _begin_apply(self):
        """Turn the proposal into a progress view. Closing is off for the
        duration: render_active_set has no abort, and a Mods/ folder caught
        half-rendered is worse than waiting. Every row's toggle (if it had
        one) is swapped for the same static, colour-coded label a required
        row always had - set_step keeps that label live as things happen."""
        self._applying = True
        self.apply_attempted = True
        self._steps_done = 0
        self._steps_total = render_plan_steps(self._plan)
        for mod_id, (cell, label, toggle) in self._action_widgets.items():
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

    # Driven by whoever runs the render, from the UI thread.

    def set_status(self, message):
        """The line of detail under the table: which file, how far into it."""
        if self._closed:
            return
        self._status.set(message)

    def set_step(self, item_id, state):
        """One item starting or finishing, as render_active_set reports them.
        Ids with no row of their own (libraries) still move the bar; they just
        have nothing to mark."""
        if self._closed:
            return
        if state == "done":
            self._steps_done += 1
            self._draw_bar()
        if item_id in self._rows:
            self._progress[item_id] = state
            if item_id in self._action_widgets:
                _, label, _ = self._action_widgets[item_id]
                label.config(text=self._action_text(item_id), fg=self._row_color(item_id))

    def apply_finished(self, ok, message=""):
        """The render is over. On success with close_on_success the window gets
        out of the way so the caller can carry on; otherwise the outcome stays
        on screen, which is the whole point of not using a popup."""
        if self._closed:
            return
        self._applying = False
        self.result = bool(ok)
        if ok:
            self._steps_done = self._steps_total
            self._draw_bar()
        if ok and self._close_on_success:
            self.destroy()
            return
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._status_lbl.config(fg=GREEN if ok else RED)
        self._status.set(message or ("Done." if ok else "Couldn't apply the changes."))
        self._apply_btn.config(state="disabled", text="Applied" if ok else "Not applied")
        self._cancel_btn.config(state="normal", text="Close")

    def _on_cancel(self):
        if self._applying:
            return
        self.result = False
        self.destroy()

    def _on_close(self):
        self.destroy()

    def destroy(self):
        # on_cancel means "the caller isn't getting an applied plan", so it
        # fires for a cancel, the X, and closing after a failed apply, but never
        # after a successful one. Once only, whichever of those happens.
        first = not self._closed
        self._closed = True
        super().destroy()
        if first and not self.result and self._on_cancel_cb:
            self._on_cancel_cb(self)
