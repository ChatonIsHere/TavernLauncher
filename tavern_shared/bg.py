"""Run slow work on a daemon thread and hand the result back on the Tk
thread, skipping a window destroyed meanwhile."""
import threading
import tkinter as tk


def post(widget, fn, *args):
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
    """An exception with no on_err goes to threading's excepthook."""
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
