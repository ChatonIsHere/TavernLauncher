"""Player-facing support ticket window."""
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
import threading, time, json, urllib.request

from tavern_shared.theme import (
    BG, SURF, SURF2, BORDER, AMBER, AMBERDIM, PARCH, MUTED, GREEN, RED, CYAN,
    _btn, _field, _hint, _section_label, _mk_tree, _mk_scrollbar, _fix_combobox_popdown_colors,
)
from tavern_shared.window_chrome import _start_hidden, _finish_dark_window, _set_window_icon

from client.core.config import load_cfg, save_cfg, _mark_tickets_seen, _remember_ticket_state, _get_last_ticket_state
from client.core.auth import ticket_request, _resolve_ip_for_game, AUTH_PORT
from client.core.config import _get_or_create_token
from client.core.server_list_panel import ServerListPanel

class TicketsWindow(tk.Toplevel):
    """Player-facing support tickets — create one, see the server owner's
    replies, respond, or close it yourself. Tied to whichever server +
    username the player is actually using, since a ticket only means
    anything in the context of one specific server's own ticket database."""
    def __init__(self, parent, default_host="", default_username=""):
        super().__init__(parent)
        _start_hidden(self)
        self.title("Support Tickets")
        self.configure(bg=BG)
        self.geometry("640x600")
        self.resizable(True, True)
        _set_window_icon(self)
        # Guarded rather than called unconditionally -- ttk.Style() is a
        # single global object shared across the whole app, and
        # unconditionally re-invoking theme_use() every time any window
        # opens (even redundantly re-setting the SAME theme) can reset
        # custom style configurations already applied elsewhere (e.g. a
        # combobox styled by an already-open window), which is almost
        # certainly why collapsed comboboxes intermittently reverted to
        # plain white once more than one window was involved.
        if ttk.Style().theme_use() != "clam":
            ttk.Style().theme_use("clam")
        self._tickets = []
        self._selected_ticket = None
        # Remembering which server/username you were last looking at
        # tickets for takes priority over whatever's currently in the
        # main launcher's own destination fields -- you're coming back
        # to check on a specific application, not necessarily about to
        # join that same server again right now.
        last_host, last_username, self._recent_hosts = _get_last_ticket_state()
        self._build(last_host or default_host, last_username or default_username)
        _finish_dark_window(self)
        self._refresh(silent=True)
        # Same reasoning as the main launcher windows — start at exactly
        # what the fully-built layout needs, then set that as the floor,
        # so shrinking the window can never clip the Reply/Close Ticket row.
        self.update_idletasks()
        fit_w = max(640, self.winfo_reqwidth())
        fit_h = max(600, self.winfo_reqheight())
        self.geometry(f"{fit_w}x{fit_h}")
        self.minsize(fit_w, fit_h)

    def _build(self, default_host, default_username):
        h = tk.Frame(self, bg=SURF, height=44)
        h.pack(fill="x"); h.pack_propagate(False)
        tk.Label(h, text="🎫  Support Tickets", bg=SURF, fg=AMBER,
                 font=("Georgia",12,"bold")).pack(side="left", padx=16, pady=8)
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        sf = tk.Frame(self, bg=BG)
        sf.pack(fill="x", padx=16, pady=(10,4))
        tk.Label(sf, text="Server:", bg=BG, fg=MUTED, font=("Segoe UI",9)).pack(side="left")
        self.v_host = tk.StringVar(value=default_host)
        style = ttk.Style()
        style.configure("Tav.TicketHost.TCombobox", fieldbackground=SURF, background=SURF2,
                        foreground=PARCH, selectbackground=SURF, selectforeground=PARCH,
                        arrowcolor=AMBERDIM, borderwidth=0)
        # configure() alone isn't enough here on Windows -- ttk's native
        # theme overrides those colors for specific widget states unless
        # explicitly mapped. _mk_combobox only ever needs to cover the
        # "readonly" state since it forces that; this one is editable, so
        # the map needs to cover the normal/editable state too.
        style.map("Tav.TicketHost.TCombobox",
                  fieldbackground=[("readonly",SURF), ("!disabled",SURF)],
                  foreground=[("readonly",PARCH), ("!disabled",PARCH)],
                  selectbackground=[("readonly",SURF), ("!disabled",SURF)],
                  selectforeground=[("readonly",PARCH), ("!disabled",PARCH)])
        host_combo = ttk.Combobox(sf, textvariable=self.v_host, values=self._recent_hosts,
                                   font=("Consolas",10), style="Tav.TicketHost.TCombobox", width=20)
        host_combo.pack(side="left", padx=(6,10))
        _fix_combobox_popdown_colors(host_combo, SURF, PARCH, AMBERDIM, "#ffd080")
        tk.Label(sf, text="Username:", bg=BG, fg=MUTED, font=("Segoe UI",9)).pack(side="left")
        self.v_username = tk.StringVar(value=default_username)
        tk.Entry(sf, textvariable=self.v_username, bg=SURF, fg=PARCH,
                 insertbackground=AMBER, relief="flat", font=("Consolas",10),
                 bd=6, width=14).pack(side="left", padx=(6,0))
        _btn(sf, "⚑ Pick Saved", self._pick_saved_server,
             font=("Segoe UI",8), pady=4, padx=6).pack(side="left", padx=(8,0))
        _hint(self, "Tickets are tied to whichever username+server you actually play on.")

        br = tk.Frame(self, bg=BG)
        br.pack(fill="x", padx=16, pady=(0,8))
        _btn(br, "⟳ Refresh My Tickets", self._refresh, "primary",
             font=("Segoe UI",9), pady=6, padx=10).pack(side="left")
        _btn(br, "+ New Ticket", self._new_ticket,
             font=("Segoe UI",9), pady=6, padx=10).pack(side="left", padx=(6,0))

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=16, pady=(0,10))

        # A fixed-height outer frame for the tree — _mk_tree's own wrapper
        # always requests expand=True internally, which would otherwise
        # compete with `detail` below for space and squeeze out the Reply/
        # Close Ticket row. The tree only ever needs to show a short list,
        # so it gets just its natural size; `detail` (below) claims
        # whatever's actually left over.
        tree_container = tk.Frame(body, bg=BG)
        tree_container.pack(fill="x")
        self.tree = _mk_tree(tree_container, ("title","status","updated"), [280,80,140], height=7)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        detail = tk.Frame(body, bg=SURF, highlightbackground=BORDER, highlightthickness=1)
        detail.pack(fill="both", expand=True, pady=(8,0))

        self.v_detail_title = tk.StringVar(value="Select a ticket, or open a new one.")
        tk.Label(detail, textvariable=self.v_detail_title, bg=SURF, fg=AMBER,
                 font=("Georgia",10,"bold"), wraplength=560, justify="left"
                 ).pack(anchor="w", padx=10, pady=(10,4))

        thread_frame = tk.Frame(detail, bg=BG)
        thread_frame.pack(fill="both", expand=True, padx=10, pady=(0,6))
        self.thread_text = tk.Text(thread_frame, bg=SURF2, fg=PARCH, relief="flat",
                                   bd=0, wrap="word", state="disabled",
                                   font=("Segoe UI",9), height=8)
        tsb = _mk_scrollbar(thread_frame, self.thread_text.yview)
        tsb.pack(side="right", fill="y")
        self.thread_text.config(yscrollcommand=tsb.set)
        self.thread_text.pack(side="left", fill="both", expand=True)
        self.thread_text.tag_config("player", foreground=CYAN)
        self.thread_text.tag_config("owner", foreground=AMBER)
        self.thread_text.tag_config("meta", foreground=MUTED)

        action_row = tk.Frame(detail, bg=SURF)
        action_row.pack(fill="x", padx=10, pady=(0,10))
        self.v_reply = tk.StringVar()
        tk.Entry(action_row, textvariable=self.v_reply, bg=SURF2, fg=PARCH,
                 insertbackground=AMBER, relief="flat",
                 highlightbackground=BORDER, highlightcolor=AMBER, highlightthickness=1,
                 font=("Consolas",9), bd=6).pack(side="left", fill="x", expand=True)
        _btn(action_row, "Reply", self._respond, font=("Segoe UI",9),
             pady=6, padx=8).pack(side="left", padx=(6,0))
        _btn(action_row, "Close Ticket", self._close_ticket, "danger",
             font=("Segoe UI",9), pady=6, padx=8).pack(side="left", padx=(6,0))

    def _current_host_username(self, silent=False):
        host = self.v_host.get().strip()
        username = self.v_username.get().strip()
        if not host or not username:
            if not silent:
                messagebox.showerror("Missing info",
                    "Enter both a server and a username.", parent=self)
            return None, None
        return host, username

    def _pick_saved_server(self):
        def on_select(ip, name, port="1757"):
            self.v_host.set(ip)
            self._refresh(silent=True)
        ServerListPanel(self, on_select)

    def _refresh(self, silent=False):
        host, username = self._current_host_username(silent=silent)
        if not host: return
        _remember_ticket_state(host, username)
        resolved = _resolve_ip_for_game(host)
        token, _ = _get_or_create_token(resolved, username)
        def worker():
            try:
                resp = ticket_request(resolved, "list_mine", username, token)
            except Exception as e:
                if not silent:
                    self.after(0, lambda e=e: messagebox.showerror(
                        "Couldn't fetch tickets", str(e), parent=self))
                return
            if resp.get("status") != "ok":
                # Most common cause here: this username+server combo has
                # never actually joined the server, so there's nothing to
                # recognize yet — completely normal the first time this
                # window is opened, not worth surfacing as an error unless
                # the player explicitly clicked Refresh to ask.
                if not silent:
                    self.after(0, lambda: messagebox.showerror(
                        "Error", resp.get("message","Unknown error"), parent=self))
                return
            self.after(0, lambda: self._apply_tickets(resp.get("tickets", []), resolved))
        threading.Thread(target=worker, daemon=True).start()

    def _apply_tickets(self, tickets, host):
        self._tickets = tickets
        for r in self.tree.get_children(): self.tree.delete(r)
        for t in sorted(tickets, key=lambda t: t["updated_at"], reverse=True):
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(t["updated_at"]))
            self.tree.insert("", "end", iid=t["ticket_id"], values=(t["title"], t["status"], ts))
        _mark_tickets_seen(host, tickets)

    def _on_select(self, _=None):
        sel = self.tree.selection()
        if not sel: return
        t = next((x for x in self._tickets if x["ticket_id"] == sel[0]), None)
        if not t: return
        self._selected_ticket = t
        self.v_detail_title.set(f"{t['title']}  ({t['status']})")
        self.thread_text.config(state="normal")
        self.thread_text.delete("1.0", "end")
        self.thread_text.insert("end", t["description"] + "\n\n")
        for c in t.get("comments", []):
            who = "Server Owner" if c["from"] == "owner" else "You"
            tag = "owner" if c["from"] == "owner" else "player"
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(c["at"]))
            self.thread_text.insert("end", f"[{ts}] {who}: ", tag)
            self.thread_text.insert("end", f"{c['message']}\n")
        self.thread_text.see("end")
        self.thread_text.config(state="disabled")

    def _new_ticket(self):
        host, username = self._current_host_username()
        if not host: return

        win = tk.Toplevel(self)
        _start_hidden(win)
        win.title("New Ticket")
        win.configure(bg=BG)
        win.resizable(False, False)
        _set_window_icon(win)

        _section_label(win, "TITLE")
        tf = _field(win)
        v_title = tk.StringVar()
        tk.Entry(tf, textvariable=v_title, bg=SURF, fg=PARCH,
                 insertbackground=AMBER, relief="flat", font=("Consolas",10),
                 bd=6).pack(fill="x")

        _section_label(win, "DESCRIPTION")
        df = tk.Frame(win, bg=SURF, highlightbackground=BORDER, highlightthickness=1)
        df.pack(fill="both", expand=True, padx=20, pady=(0,6))
        desc_text = tk.Text(df, bg=SURF, fg=PARCH, insertbackground=AMBER,
                            relief="flat", bd=6, wrap="word", height=8,
                            font=("Segoe UI",9))
        desc_text.pack(fill="both", expand=True)

        def _submit():
            title = v_title.get().strip()
            description = desc_text.get("1.0","end").strip()
            if not title or not description:
                messagebox.showerror("Missing info",
                    "Title and description are both required.", parent=win)
                return
            resolved = _resolve_ip_for_game(host)
            token, _ = _get_or_create_token(resolved, username)
            def worker():
                try:
                    resp = ticket_request(resolved, "create", username, token,
                                          title=title, description=description, server=host)
                except Exception as e:
                    self.after(0, lambda e=e: messagebox.showerror(
                        "Couldn't submit ticket", str(e), parent=win))
                    return
                if resp.get("status") != "ok":
                    self.after(0, lambda: messagebox.showerror(
                        "Error", resp.get("message","Unknown error"), parent=win))
                    return
                def _done():
                    win.destroy()
                    self._refresh()
                self.after(0, _done)
            threading.Thread(target=worker, daemon=True).start()

        tk.Frame(win, bg=BORDER, height=1).pack(fill="x", padx=20, pady=(4,6))
        _btn(win, "📨  Submit Ticket", _submit, "primary",
             font=("Georgia",11,"bold"), pady=12).pack(fill="x", padx=20, pady=(0,16))

        win.update_idletasks()
        win.geometry(f"420x{win.winfo_reqheight()}")
        _finish_dark_window(win)
        win.transient(self)
        win.grab_set()

    def _selected_ticket_id(self):
        if not self._selected_ticket:
            messagebox.showinfo("No selection", "Select a ticket first.", parent=self)
            return None
        return self._selected_ticket["ticket_id"]

    def _respond(self):
        tid = self._selected_ticket_id()
        if not tid: return
        msg = self.v_reply.get().strip()
        if not msg: return
        if self._selected_ticket.get("status") != "open":
            messagebox.showinfo("Ticket closed", "This ticket is already closed.", parent=self)
            return
        host, username = self._current_host_username()
        resolved = _resolve_ip_for_game(host)
        token, _ = _get_or_create_token(resolved, username)
        def worker():
            try:
                resp = ticket_request(resolved, "respond", username, token,
                                      ticket_id=tid, message=msg)
            except Exception as e:
                self.after(0, lambda e=e: messagebox.showerror(
                    "Couldn't send reply", str(e), parent=self))
                return
            if resp.get("status") != "ok":
                self.after(0, lambda: messagebox.showerror(
                    "Error", resp.get("message","Unknown error"), parent=self))
                return
            def _done():
                self.v_reply.set("")
                self._refresh()
            self.after(0, _done)
        threading.Thread(target=worker, daemon=True).start()

    def _close_ticket(self):
        tid = self._selected_ticket_id()
        if not tid: return
        if self._selected_ticket.get("status") != "open":
            messagebox.showinfo("Already closed", "This ticket is already closed.", parent=self)
            return
        msg = simpledialog.askstring("Close Ticket",
            "Optional closing message (e.g. \"fixed it myself\"):", parent=self) or ""
        if not messagebox.askyesno("Close Ticket",
                "Close this ticket? You won't be able to reply to it afterward.", parent=self):
            return
        host, username = self._current_host_username()
        resolved = _resolve_ip_for_game(host)
        token, _ = _get_or_create_token(resolved, username)
        def worker():
            try:
                resp = ticket_request(resolved, "close", username, token,
                                      ticket_id=tid, message=msg)
            except Exception as e:
                self.after(0, lambda e=e: messagebox.showerror(
                    "Couldn't close ticket", str(e), parent=self))
                return
            if resp.get("status") != "ok":
                self.after(0, lambda: messagebox.showerror(
                    "Error", resp.get("message","Unknown error"), parent=self))
                return
            self.after(0, self._refresh)
        threading.Thread(target=worker, daemon=True).start()

