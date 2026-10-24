"""Background work for Tk windows: run something slow on a daemon thread and
hand its result back on the Tk thread, or not at all if the window has gone.

Tk widgets may only be touched from the thread running mainloop, so a worker
never calls back into one directly; everything goes through widget.after(0).
A window closed while its worker ran is skipped rather than called into."""
import threading
import tkinter as tk


def post(widget, fn, *args):
    """Calls fn(*args) on widget's Tk thread, unless widget is destroyed by
    then. Safe from any thread."""
    def call():
        try:
            alive = widget.winfo_exists()
        except tk.TclError:
            return
        if alive:
            fn(*args)
    try:
        widget.after(0, call)
    except (RuntimeError, tk.TclError):
        pass  # the whole app is shutting down


def run_bg(widget, fn, on_ok=None, on_err=None):
    """Runs fn() on a daemon thread, then on_ok(result) or on_err(exception)
    on widget's Tk thread. An exception with no on_err propagates in the
    worker, where threading's excepthook reports it."""
    def worker():
        try:
            result = fn()
        except Exception as e:
            if on_err is None:
                raise
            post(widget, on_err, e)
            return
        if on_ok is not None:
            post(widget, on_ok, result)
    threading.Thread(target=worker, daemon=True).start()
