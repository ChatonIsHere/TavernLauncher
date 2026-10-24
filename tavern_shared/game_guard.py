"""Asks "is the game holding these files open?" before the launcher writes into
the game folder. Windows keeps loaded assemblies locked, so a write while the
game runs fails partway with a raw PermissionError; asking first, while
nothing has been touched, is the fix. The only signal is the process this
launcher started, so a game launched some other way isn't detected, and the
prompt can be overridden ("Try anyway?") rather than hard-blocking."""
from tkinter import messagebox


def game_is_running(proc):
    """True while `proc` (a subprocess.Popen, or None) is still alive."""
    return bool(proc and proc.poll() is None)


def any_game_running(procs):
    """True while any of `procs` is alive, pruning exited ones in place. A list,
    because an overridden prompt can start a second copy while the first still
    holds files."""
    procs[:] = [p for p in procs if game_is_running(p)]
    return bool(procs)


def confirm_while_game_running(parent, is_game_running, action):
    """Whether `action` (e.g. "Installing a mod") should go ahead: True at once
    when there's no check or the game isn't running, else a prompt that
    defaults to no. The server launcher passes no check (see its
    _on_mods_changed)."""
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
