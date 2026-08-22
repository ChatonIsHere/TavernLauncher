"""One answer to "is the game holding these files open right now?", asked
everywhere the launcher is about to write into the game folder.

Windows keeps a loaded assembly open for as long as the process that loaded it
lives, so an install, an uninstall, or a per-server render while A Township Tale
is running fails somewhere in the middle of a rmtree/replace - and what the
player sees is a raw PermissionError with a path in it, from a worker thread,
after the operation has already half-happened. The fix isn't to catch that
error (by then a mod folder can be partly gone); it's to ask first, while
nothing has been touched yet.

The signal is the process THIS launcher started - the one the join flow spawns
and keeps a handle on. A game the player started some other way is not detected
and never will be by this route; that's the ordinary case failing loudly
instead of the common one failing confusingly, which is the trade being made.
Hence "Try anyway?" rather than a hard block: the check is honest about being
one signal, not proof.
"""
from tkinter import messagebox


def game_is_running(proc):
    """True while `proc` (a subprocess.Popen, or None) is still alive."""
    return bool(proc and proc.poll() is None)


def any_game_running(procs):
    """True while any of `procs` is still alive, pruning the exited ones from
    the list in place. A list, not one handle: the prompt below can be
    overridden and a second copy launched, and the guard has to keep watching
    the first for as long as it lives, not just the newest."""
    procs[:] = [p for p in procs if game_is_running(p)]
    return bool(procs)


def confirm_while_game_running(parent, is_game_running, action):
    """Whether `action` should go ahead. True immediately when nothing says the
    game is running (including when the caller passes no check at all - the
    server launcher has its own, different story about this, see its
    _on_mods_changed). Otherwise asks, defaulting to no.

    `action` is a capitalised phrase completing "<action> while it's running",
    e.g. "Installing a mod".
    """
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
