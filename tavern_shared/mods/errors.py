"""The mod manager's one error type, plus the non-fatal warn channel."""
import sys


class ModManagerError(Exception):
    """The mod manager's one error type, raised with a message the UI can show
    verbatim."""


def _warn(msg):
    """Non-fatal problems go to stderr, so one bad source doesn't break the rest."""
    try:
        print(f"[modmanager] {msg}", file=sys.stderr)
    except Exception:
        pass
