"""Stand-ins for the Tk pieces the launcher tests bind methods against, so
those tests never need a display."""


class FakeWidget:
    def config(self, **kwargs):
        pass


class FakeVar:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class InlineThread:
    """threading.Thread that runs its target on start(), so a test sees the
    worker's effects without a join or a timeout."""

    def __init__(self, target=None, args=(), daemon=None):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


class NoThread(InlineThread):
    """threading.Thread whose target never runs (e.g. a watcher that would
    block on a real process)."""

    def start(self):
        pass


def inline_run_bg(widget, fn, on_ok=None, on_err=None):
    """tavern_shared.bg.run_bg, run synchronously on the calling thread."""
    try:
        result = fn()
    except Exception as e:
        if on_err is None:
            raise
        on_err(e)
        return
    if on_ok is not None:
        on_ok(result)
