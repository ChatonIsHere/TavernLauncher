"""Asks before writing into a game folder the running game has locked. Only
a game this launcher started is detected, so the prompt can be overridden."""
from tkinter import messagebox


def game_is_running(proc):
    return bool(proc and proc.poll() is None)


def any_game_running(procs):
    """Prunes exited processes from `procs` in place."""
    procs[:] = [p for p in procs if game_is_running(p)]
    return bool(procs)


def confirm_while_game_running(parent, is_game_running, action):
    """Whether `action` should go ahead; the server launcher passes no check."""
    if not is_game_running or not is_game_running():
        return True
    return messagebox.askyesno(
        "The game is running",
        "A Township Tale is open right now, and Windows keeps the files it has "
        f"loaded locked for as long as it is.\n\n{action} while it's running "
        "usually fails partway through, and anything that does get changed "
        "won't take effect until the game is restarted anyway.\n\n"
        "Close the game, then try again.\n\nTry anyway?",
        icon="warning", default="no", parent=parent)
